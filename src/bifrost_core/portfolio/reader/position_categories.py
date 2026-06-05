"""Position categories CRUD: read and write preference_position_categories / preference_position_category_tags."""

import logging
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


def _pg_exc_message(exc: BaseException) -> str:
    if isinstance(exc, psycopg2.Error):
        detail = getattr(exc, "diag", None)
        if detail is not None and getattr(detail, "message_primary", None):
            return str(detail.message_primary).strip()
        if getattr(exc, "pgerror", None):
            return str(exc.pgerror).strip()
    return str(exc).strip()[:500]


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


def create_position_category(
    conn: Any,
    name: str,
    description: Optional[str] = None,
    sort_order: Optional[int] = None,
) -> Tuple[Optional[int], Optional[str]]:
    """Returns (new_id, error_message). error_message is set only on failure."""
    if not name or not str(name).strip() or conn is None:
        return None, "Invalid name or no database connection."
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO preference_position_categories (name, description, sort_order, updated_at)
                VALUES (%s, %s, %s, now())
                RETURNING id
                """,
                (str(name).strip(), (description or "").strip() or None, sort_order),
            )
            row = cur.fetchone()
        conn.commit()
        if row and row[0] is not None:
            return int(row[0]), None
        return None, "Insert returned no id."
    except Exception as e:
        msg = _pg_exc_message(e)
        logger.warning("create_position_category failed: %s", msg)
        try:
            conn.rollback()
        except Exception:
            pass
        return None, msg or "Database error."


def update_position_category(
    conn: Any,
    category_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    sort_order: Optional[int] = None,
) -> bool:
    if conn is None:
        return False
    try:
        updates = ["updated_at = now()"]
        vals: List[Any] = []
        if name is not None:
            updates.append("name = %s")
            vals.append(str(name).strip() if str(name).strip() else None)
        if description is not None:
            updates.append("description = %s")
            vals.append(str(description).strip() or None)
        if sort_order is not None:
            updates.append("sort_order = %s")
            vals.append(sort_order)
        if not vals:
            return True
        vals.append(category_id)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE preference_position_categories SET {', '.join(updates)} WHERE id = %s",
                tuple(vals),
            )
        conn.commit()
        return True
    except Exception as e:
        logger.debug("update_position_category failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def delete_position_category(conn: Any, category_id: int) -> bool:
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM preference_position_categories WHERE id = %s", (category_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.debug("delete_position_category failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def set_position_category_tag(
    conn: Any,
    account_id: str,
    contract_key: str,
    category_id: Optional[int],
) -> bool:
    if not account_id or not str(account_id).strip() or not contract_key or not str(contract_key).strip() or conn is None:
        return False
    try:
        acc = str(account_id).strip()
        ck = str(contract_key).strip()
        with conn.cursor() as cur:
            if category_id is None:
                cur.execute(
                    "DELETE FROM preference_position_category_tags WHERE account_id = %s AND contract_key = %s",
                    (acc, ck),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO preference_position_category_tags (account_id, contract_key, category_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (account_id, contract_key) DO UPDATE SET category_id = EXCLUDED.category_id
                    """,
                    (acc, ck, category_id),
                )
        conn.commit()
        return True
    except Exception as e:
        logger.debug("set_position_category_tag failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


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


def set_market_streams_symbol_order(
    conn: Any,
    category_name: str,
    symbols: List[str],
) -> bool:
    """Replace symbol order for one category. symbols = ordered list of symbol strings."""
    if conn is None:
        return False
    cat = (category_name or "").strip()
    if not cat:
        return False
    symbols_clean = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM preference_market_streams_symbol_order WHERE category_name = %s",
                (cat,),
            )
            for i, sym in enumerate(symbols_clean):
                cur.execute(
                    """
                    INSERT INTO preference_market_streams_symbol_order (category_name, symbol, sort_order, updated_at)
                    VALUES (%s, %s, %s, now())
                    """,
                    (cat, sym, i),
                )
        conn.commit()
        return True
    except Exception as e:
        logger.debug("set_market_streams_symbol_order failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
