"""Watchlist: conn-based read/write. Used by common.StatusReader.

``upsert_watchlist``, ``patch_watchlist_item`` and ``delete_watchlist_strict`` (core
0.33.0, TD-15) take exactly the fields the client sent, return the row and raise
``Write*``; ``add_watchlist`` / ``delete_watchlist`` answer a bool for one release.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteFailed, WriteInvalid, WriteNotFound

logger = logging.getLogger(__name__)


def get_watchlist(conn: Any) -> List[Dict[str, Any]]:
    """Return all watchlist rows (contract_key, symbol, sec_type, ..., category_id, category, optionable, created_at)."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT w.contract_key, w.symbol, w.sec_type, w.expiry, w.strike, w.option_right,
                       w.display_label, w.source, w.category_id, w.optionable,
                       pc.name AS category,
                       extract(epoch from w.created_at) AS created_at
                FROM watchlist w
                LEFT JOIN preference_position_categories pc ON w.category_id = pc.id
                ORDER BY w.created_at DESC
                """
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.debug("get_watchlist failed: %s", e)
        return []


_WATCHLIST_VALUE_COLUMNS = (
    "symbol",
    "sec_type",
    "expiry",
    "strike",
    "option_right",
    "display_label",
    "source",
    "category_id",
)


def add_watchlist(
    conn: Any,
    contract_key: str,
    symbol: Optional[str] = None,
    sec_type: Optional[str] = None,
    expiry: Optional[str] = None,
    strike: Optional[float] = None,
    option_right: Optional[str] = None,
    display_label: Optional[str] = None,
    source: Optional[str] = None,
    category_id: Optional[int] = None,
    optionable: Optional[bool] = None,
    *,
    clear: Iterable[str] = (),
) -> bool:
    """Insert a watchlist row, or update the one with this contract_key. Returns True on success.

    If contract_key contains no '|', treat as stock symbol and normalize to SYMBOL|STK|||.

    On UPDATE a None keeps what is stored, for every column (core 0.33.0). Until then
    only ``optionable`` was kept: re-adding a watched symbol from the Omnibar, the
    Symbol Dock or a drop -- which send no ``category_id`` and no ``display_label`` --
    moved it out of its list and dropped its label (TD-15). To set a column to NULL,
    name it in ``clear`` (``clear=("category_id",)`` moves the row out of its list);
    ``clear`` applies only where the value passed is None. A new row gets source
    'manual' when none is given.
    """
    raw = (contract_key or "").strip()
    if not raw:
        return False
    if "|" not in raw:
        contract_key = f"{raw}|STK|||"
        if symbol is None:
            symbol = raw
        if sec_type is None or sec_type == "":
            sec_type = "STK"
    else:
        contract_key = raw
    to_clear = {c for c in clear if c in _WATCHLIST_VALUE_COLUMNS}
    params: Dict[str, Any] = {
        "contract_key": contract_key,
        "symbol": symbol,
        "sec_type": sec_type,
        "expiry": expiry,
        "strike": strike,
        "option_right": option_right,
        "display_label": display_label,
        "source": source,
        "category_id": category_id,
        "optionable": optionable,
    }
    updates = []
    for col in _WATCHLIST_VALUE_COLUMNS:
        if col in to_clear and params[col] is None:
            updates.append(f"{col} = NULL")
        else:
            updates.append(f"{col} = COALESCE(%({col})s, watchlist.{col})")
    updates.append("optionable = COALESCE(%(optionable)s, watchlist.optionable)")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO watchlist (contract_key, symbol, sec_type, expiry, strike, option_right, display_label, source, category_id, optionable)
                VALUES (%(contract_key)s, %(symbol)s, %(sec_type)s, %(expiry)s, %(strike)s, %(option_right)s,
                        %(display_label)s, COALESCE(%(source)s, 'manual'), %(category_id)s, %(optionable)s)
                ON CONFLICT (contract_key) DO UPDATE SET {", ".join(updates)}
                """,
                params,
            )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("add_watchlist failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def delete_watchlist(conn: Any, contract_key: Optional[str] = None) -> bool:
    """Delete one watchlist entry by contract_key. Returns True on success."""
    if not contract_key or not contract_key.strip():
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watchlist WHERE contract_key = %s", (contract_key.strip(),))
        conn.commit()
        return True
    except Exception as e:
        logger.debug("delete_watchlist failed: %s", e)
        return False


# --- TD-15 writers (core 0.33.0): return the row / raise Write* ---------------------

WATCHLIST_PATCHABLE = (*_WATCHLIST_VALUE_COLUMNS, "optionable")

_WATCHLIST_ROW_SELECT = """
    SELECT w.contract_key, w.symbol, w.sec_type, w.expiry, w.strike, w.option_right,
           w.display_label, w.source, w.category_id, w.optionable,
           pc.name AS category,
           extract(epoch from w.created_at) AS created_at
    FROM watchlist w
    LEFT JOIN preference_position_categories pc ON w.category_id = pc.id
    WHERE w.contract_key = %s
"""


def _watchlist_key(contract_key: Any) -> Tuple[str, Optional[str]]:
    """(stored key, the bare symbol it was given as -- or None). 'AAPL' is 'AAPL|STK|||'."""
    raw = str(contract_key or "").strip()
    if not raw:
        raise WriteInvalid("contract_key is required.")
    if "|" not in raw:
        return f"{raw}|STK|||", raw
    return raw, None


def _watchlist_columns(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the sent fields. Every column but ``optionable`` is nullable (null clears);
    blank text is refused; ``optionable`` is true or false."""
    cols: Dict[str, Any] = {}
    for name in ("symbol", "sec_type", "expiry", "option_right", "display_label", "source"):
        if name in fields:
            cols[name] = ws.text(fields[name], name, nullable=True)
    if "strike" in fields:
        cols["strike"] = ws.number(fields["strike"], "strike", nullable=True, minimum=0)
    if "category_id" in fields:
        cols["category_id"] = ws.row_id(fields["category_id"], "category_id", nullable=True)
    if "optionable" in fields:
        cols["optionable"] = ws.boolean(fields["optionable"], "optionable")
    return cols


def _read_watchlist_row(conn: Any, contract_key: str, what: str) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(_WATCHLIST_ROW_SELECT, (contract_key,))
        row = cur.fetchone()
    if row is None:
        raise WriteFailed(f"{what} was written but could not be read back; nothing was saved.")
    return dict(row)


def upsert_watchlist(conn_or_config: Any, contract_key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Add a contract to the watchlist, or change the one already there; return the row in ``get_watchlist``'s shape.

    ``fields`` is exactly what the client sent (it may be empty: a bare add). On an
    existing row only the sent fields change -- a field left out keeps its value, an
    explicit null clears it. A new row takes the sent fields; a bare symbol key
    ('AAPL') becomes 'AAPL|STK|||' with symbol / sec_type filled when not sent, and
    source is 'manual' when not sent. Raises WriteInvalid (unknown key, bad value,
    unknown category_id), WriteFailed.
    """
    key, bare_symbol = _watchlist_key(contract_key)
    what = f"watchlist item {key}"
    if not isinstance(fields, dict):
        raise WriteInvalid("The watchlist fields must be an object.")
    if fields:
        ws.check_fields(fields, WATCHLIST_PATCHABLE, "watchlist")
    cols = _watchlist_columns(fields)
    insert = {"contract_key": key, **cols}
    if bare_symbol is not None:
        insert.setdefault("symbol", bare_symbol)
        insert.setdefault("sec_type", "STK")
    insert.setdefault("source", "manual")
    updates = [f"{c} = EXCLUDED.{c}" for c in cols] or ["contract_key = EXCLUDED.contract_key"]
    sql = (
        f"INSERT INTO watchlist ({', '.join(insert)}) VALUES ({', '.join(['%s'] * len(insert))}) "
        f"ON CONFLICT (contract_key) DO UPDATE SET {', '.join(updates)}"
    )
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute(sql, list(insert.values()))
        row = _read_watchlist_row(conn, key, what)
    return row


def patch_watchlist_item(conn_or_config: Any, contract_key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change a watched contract's fields; return the row in ``get_watchlist``'s shape.

    Does not insert: a contract not on the list is WriteNotFound. Patchable: symbol,
    sec_type, expiry, strike, option_right, display_label, source, category_id (nullable;
    null clears) and optionable (true / false). Raises WriteInvalid, WriteNotFound, WriteFailed.
    """
    key, _ = _watchlist_key(contract_key)
    what = f"watchlist item {key}"
    fields = ws.check_fields(fields, WATCHLIST_PATCHABLE, "watchlist")
    cols = _watchlist_columns(fields)
    assignments, values = ws.set_clause(cols, touch=False)
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute(f"UPDATE watchlist SET {assignments} WHERE contract_key = %s", [*values, key])
            if cur.rowcount == 0:
                raise WriteNotFound(f"{key} is not on the watchlist.")
        row = _read_watchlist_row(conn, key, what)
    return row


def delete_watchlist_strict(conn_or_config: Any, contract_key: str) -> Dict[str, Any]:
    """Take a contract off the watchlist. Returns ``{"deleted": "hard", "contract_key"}``;
    WriteNotFound when it was not on the list (``delete_watchlist`` answered True), WriteFailed."""
    key, _ = _watchlist_key(contract_key)
    what = f"watchlist item {key}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watchlist WHERE contract_key = %s", (key,))
            if cur.rowcount == 0:
                raise WriteNotFound(f"{key} is not on the watchlist.")
    return {"deleted": "hard", "contract_key": key}
