"""Watchlist: conn-based read/write. Used by common.StatusReader.

``upsert_watchlist``, ``patch_watchlist_item`` and ``delete_watchlist_strict`` (core
0.33.0, TD-15) take exactly the fields the client sent, return the row and raise
``Write*``. The bool writers ``add_watchlist`` / ``delete_watchlist`` left in core 0.46.0 (TD-80).
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

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
    WriteNotFound when it was not on the list, WriteFailed."""
    key, _ = _watchlist_key(contract_key)
    what = f"watchlist item {key}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM watchlist WHERE contract_key = %s", (key,))
            if cur.rowcount == 0:
                raise WriteNotFound(f"{key} is not on the watchlist.")
    return {"deleted": "hard", "contract_key": key}
