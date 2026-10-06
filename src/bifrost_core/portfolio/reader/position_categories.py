"""Position categories CRUD: read and write preference_position_categories / preference_position_category_tags.

``patch_position_category`` and ``delete_position_category_strict`` (core 0.33.0,
TD-15) check the row exists and raise ``Write*``; so do ``create_position_category_strict``,
``set_position_category_tag_strict`` and ``set_market_streams_symbol_order_strict`` (core
0.47.0, TD-80 C2), which the API's POST / PUT call. The bool / ``(id, error)`` writers they
replaced left in core 0.48.0 (TD-80 C2-b), with the ``StatusReader`` facade's write methods.

Names (TD-56, core 0.41.0): ``preference_market_streams_symbol_order`` keeps each category's
symbol order under the category's *name*, so the name is a key -- UNIQUE in the table
(``preference_position_categories_name_uq``) -- and every rename or delete carries the order
along in the same transaction (rename moves the rows, delete removes them). ``Uncategorized``
is the Live page's name for positions without a category; its order rows are stored under
that name, so no category may take it (refused case-insensitively, WriteInvalid). A name
already in use is WriteConflict."""

import logging
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteConflict, WriteInvalid, WriteNotFound

logger = logging.getLogger(__name__)

# The Live page's pseudo-category for positions without one (its symbol order is stored under it).
UNCATEGORIZED = "Uncategorized"
RESERVED_CATEGORY_NAMES = (UNCATEGORIZED,)


def check_category_name(name: str) -> None:
    """WriteInvalid when ``name`` is reserved (``Uncategorized``, in any case)."""
    if name.strip().casefold() in {n.casefold() for n in RESERVED_CATEGORY_NAMES}:
        raise WriteInvalid(f"'{name.strip()}' is reserved for positions without a category; choose another name.")


def _refuse_taken_name(cur: Any, name: str, category_id: Optional[int] = None) -> None:
    """WriteConflict when another category already has ``name`` (the UNIQUE says so too)."""
    cur.execute(
        "SELECT 1 FROM preference_position_categories WHERE name = %s AND id IS DISTINCT FROM %s",
        (name, category_id),
    )
    if cur.fetchone() is not None:
        raise WriteConflict(f"A position category named '{name}' already exists.")


def _carry_symbol_order(cur: Any, old_name: Optional[str], new_name: Optional[str]) -> int:
    """Move a category's symbol order to its new name, or drop it (``new_name`` None). Rows already
    stored under the new name belong to no category (the name was free) and are replaced."""
    if not old_name or old_name == new_name:
        return 0
    if new_name is None:
        cur.execute("DELETE FROM preference_market_streams_symbol_order WHERE category_name = %s", (old_name,))
        return int(cur.rowcount or 0)
    cur.execute("DELETE FROM preference_market_streams_symbol_order WHERE category_name = %s", (new_name,))
    cur.execute(
        "UPDATE preference_market_streams_symbol_order SET category_name = %s, updated_at = now() "
        "WHERE category_name = %s",
        (new_name, old_name),
    )
    return int(cur.rowcount or 0)


def get_position_categories(conn: Any) -> List[Dict[str, Any]]:
    if conn is None:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, name, description, sort_order, created_at, updated_at
                FROM preference_position_categories
                ORDER BY COALESCE(sort_order, 999), name
                """
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.debug("get_position_categories failed: %s", e)
        return []


def get_market_streams_symbol_order(conn: Any) -> Dict[str, List[str]]:
    """Return category_name -> ordered list of symbols from preference_market_streams_symbol_order."""
    if conn is None:
        return {}
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT category_name, symbol, sort_order
                FROM preference_market_streams_symbol_order
                ORDER BY category_name, sort_order
                """
            )
            rows = cur.fetchall()
        out: Dict[str, List[str]] = {}
        for r in (rows or []):
            cat = (r.get("category_name") or "").strip()
            sym = (r.get("symbol") or "").strip()
            if not cat or not sym:
                continue
            if cat not in out:
                out[cat] = []
            out[cat].append(sym)
        return out
    except Exception as e:
        logger.debug("get_market_streams_symbol_order failed: %s", e)
        return {}


# --- TD-15 writers (core 0.33.0): return the row / raise Write* ---------------------

POSITION_CATEGORY_PATCHABLE = ("name", "description", "sort_order")
_CATEGORY_COLUMNS = "id, name, description, sort_order, created_at, updated_at"


def patch_position_category(conn_or_config: Any, category_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change the fields the client sent; return the row in ``get_position_categories``' shape.

    ``name`` NOT NULL text · ``description`` nullable text (null clears; blank is refused,
    where the old ``update_position_category`` stored it as NULL) · ``sort_order`` nullable whole
    number. A new name carries the category's symbol order with it, in the same transaction
    (TD-56); the reserved ``Uncategorized`` is WriteInvalid, a name in use WriteConflict.
    Raises WriteInvalid, WriteNotFound, WriteConflict, WriteFailed.
    """
    what = f"position category {category_id}"
    fields = ws.check_fields(fields, POSITION_CATEGORY_PATCHABLE, "position category")
    columns: Dict[str, Any] = {}
    if "name" in fields:
        columns["name"] = ws.text(fields["name"], "name", nullable=False)
        check_category_name(columns["name"])
    if "description" in fields:
        columns["description"] = ws.text(fields["description"], "description", nullable=True)
    if "sort_order" in fields:
        columns["sort_order"] = ws.integer(fields["sort_order"], "sort_order", nullable=True)
    assignments, values = ws.set_clause(columns)
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT name FROM preference_position_categories WHERE id = %s FOR UPDATE", (category_id,))
            old = cur.fetchone()
            if old is None:
                raise WriteNotFound(f"No position category {category_id}.")
            if "name" in columns:
                _refuse_taken_name(cur, columns["name"], category_id)
            cur.execute(
                f"UPDATE preference_position_categories SET {assignments} WHERE id = %s RETURNING {_CATEGORY_COLUMNS}",
                [*values, category_id],
            )
            row = cur.fetchone()
            if row is None:
                raise WriteNotFound(f"No position category {category_id}.")
            if "name" in columns:
                _carry_symbol_order(cur, old["name"], columns["name"])
    return dict(row)


def delete_position_category_strict(conn_or_config: Any, category_id: int) -> Dict[str, Any]:
    """Hard-delete a category. Returns ``{"deleted": "hard", "id", "tags_removed", "watchlist_uncategorized",
    "symbol_order_removed"}``.

    Nothing refuses it: its position tags go with it (CASCADE), watchlist rows in it fall
    back to no category (SET NULL) and its symbol order rows are deleted in the same
    transaction (TD-56); the counts say how many. Raises WriteNotFound, WriteFailed.
    """
    what = f"position category {category_id}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what, on_fk="conflict"):
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM preference_position_categories WHERE id = %s FOR UPDATE", (category_id,))
            found = cur.fetchone()
            if found is None:
                raise WriteNotFound(f"No position category {category_id}.")
            cur.execute("SELECT count(*) FROM preference_position_category_tags WHERE category_id = %s", (category_id,))
            tags = int((cur.fetchone() or [0])[0] or 0)
            cur.execute("SELECT count(*) FROM watchlist WHERE category_id = %s", (category_id,))
            watched = int((cur.fetchone() or [0])[0] or 0)
            cur.execute("DELETE FROM preference_position_categories WHERE id = %s", (category_id,))
            if cur.rowcount == 0:
                raise WriteNotFound(f"No position category {category_id}.")
            ordered = _carry_symbol_order(cur, found[0], None)
    return {
        "deleted": "hard",
        "id": category_id,
        "tags_removed": tags,
        "watchlist_uncategorized": watched,
        "symbol_order_removed": ordered,
    }


# --- TD-80 C2 writers (core 0.47.0): POST / PUT (the bool writers left in 0.48.0) -----------


def create_position_category_strict(
    conn_or_config: Any,
    name: Any,
    description: Any = None,
    sort_order: Any = None,
) -> Dict[str, Any]:
    """Add one category; return the row in ``get_position_categories``' shape.

    ``name`` NOT NULL text, not the reserved ``Uncategorized`` (WriteInvalid) and not one in use
    (WriteConflict) · ``description`` nullable text (blank refused, as PATCH does) ·
    ``sort_order`` nullable whole number. Raises WriteInvalid, WriteConflict, WriteFailed.
    """
    category_name = ws.text(name, "name", nullable=False)
    check_category_name(category_name)
    columns = {
        "name": category_name,
        "description": ws.text(description, "description", nullable=True),
        "sort_order": ws.integer(sort_order, "sort_order", nullable=True),
    }
    what = f"position category '{category_name}'"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _refuse_taken_name(cur, category_name)
            cur.execute(
                "INSERT INTO preference_position_categories (name, description, sort_order, updated_at) "
                f"VALUES (%s, %s, %s, now()) RETURNING {_CATEGORY_COLUMNS}",
                list(columns.values()),
            )
            row = cur.fetchone()
    return dict(row)


def set_position_category_tag_strict(
    conn_or_config: Any,
    account_id: Any,
    contract_key: Any,
    category_id: Any,
) -> Dict[str, Any]:
    """Tag one position with a category, or clear its tag (``category_id`` None).

    Returns ``{"account_id", "contract_key", "category_id", "cleared"}``; ``cleared`` says
    whether a clear removed a tag (clearing an untagged position is not an error).
    ``account_id`` / ``contract_key`` required text · ``category_id`` an existing category
    (one that does not exist is WriteInvalid -- the body names it). Raises WriteInvalid,
    WriteFailed.
    """
    account = ws.text(account_id, "account_id", nullable=False)
    key = ws.text(contract_key, "contract_key", nullable=False)
    category = ws.row_id(category_id, "category_id", nullable=True)
    what = f"the category tag of {key} in {account}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            if category is None:
                cur.execute(
                    "DELETE FROM preference_position_category_tags WHERE account_id = %s AND contract_key = %s",
                    (account, key),
                )
                return {"account_id": account, "contract_key": key, "category_id": None, "cleared": cur.rowcount > 0}
            cur.execute("SELECT 1 FROM preference_position_categories WHERE id = %s", (category,))
            if cur.fetchone() is None:
                raise WriteInvalid(f"No position category {category}.")
            cur.execute(
                """
                INSERT INTO preference_position_category_tags (account_id, contract_key, category_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (account_id, contract_key) DO UPDATE SET category_id = EXCLUDED.category_id
                """,
                (account, key, category),
            )
    return {"account_id": account, "contract_key": key, "category_id": category, "cleared": False}


def set_market_streams_symbol_order_strict(
    conn_or_config: Any,
    category_name: Any,
    symbols: Any,
) -> Dict[str, Any]:
    """Replace one category's Market Streams symbol order. Returns ``{"category_name", "symbols"}``.

    ``category_name`` required text: the order is stored under the name (``Uncategorized``
    included), so a name no category has yet is accepted as before. ``symbols`` a list of
    symbols in order, ``[]`` empties it; a blank, non-text or repeated symbol is WriteInvalid
    and nothing changes (the bool writer dropped blanks and failed on a repeat). Raises
    WriteInvalid, WriteFailed.
    """
    category = ws.text(category_name, "category_name", nullable=False)
    ordered = [
        ws.text(s, f"symbols[{i}]", nullable=False) for i, s in enumerate(ws.list_value(symbols, "symbols"))
    ]
    repeated = sorted({s for s in ordered if ordered.count(s) > 1})
    if repeated:
        raise WriteInvalid(f"symbols lists {ws.name_list(repeated)} more than once.")
    what = f"the symbol order of {category}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM preference_market_streams_symbol_order WHERE category_name = %s", (category,))
            for i, sym in enumerate(ordered):
                cur.execute(
                    "INSERT INTO preference_market_streams_symbol_order (category_name, symbol, sort_order, updated_at) "
                    "VALUES (%s, %s, %s, now())",
                    (category, sym, i),
                )
    return {"category_name": category, "symbols": ordered}
