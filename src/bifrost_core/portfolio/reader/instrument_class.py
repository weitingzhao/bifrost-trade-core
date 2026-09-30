"""Instrument class: what kind of security a stock-like holding is (preference_instrument_class).

IB books a bond or T-bill ETF as STK, and neither the broker nor the data vendor
says which funds are fixed income or cash-like, so the Owner registers it once
per instrument (trade design Rev .119, Owner-approved 2026-09-30). It is a
property of the security, not of an account: one row per `contract_key`, the
same key positions, watchlist and category tags use (STK: `SYMBOL|STK|||`).

An instrument with no row is read as a stock by the callers; nothing here
infers a class from the Owner's category.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

INSTRUMENT_CLASSES: Tuple[str, ...] = ("stock", "fixed_income", "cash_like")


def normalize_instrument_class(value: Any) -> Optional[str]:
    """The stored spelling of a class, or None when it is not one of the three."""
    s = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return s if s in INSTRUMENT_CLASSES else None


def list_instrument_classes(conn: Any) -> List[Dict[str, Any]]:
    if conn is None:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT contract_key, instrument_class, note, created_at, updated_at
                FROM preference_instrument_class
                ORDER BY contract_key
                """
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.debug("list_instrument_classes failed: %s", e)
        return []


def set_instrument_class(
    conn: Any,
    contract_key: str,
    instrument_class: str,
    note: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """Register or change one instrument's class. Returns (ok, error_message)."""
    ck = str(contract_key or "").strip()
    cls = normalize_instrument_class(instrument_class)
    if not ck:
        return False, "contract_key is required."
    if cls is None:
        return False, f"instrument_class must be one of {', '.join(INSTRUMENT_CLASSES)}."
    if conn is None:
        return False, "No database connection."
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO preference_instrument_class (contract_key, instrument_class, note, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (contract_key) DO UPDATE
                SET instrument_class = EXCLUDED.instrument_class,
                    note = COALESCE(EXCLUDED.note, preference_instrument_class.note),
                    updated_at = now()
                """,
                (ck, cls, (note or "").strip() or None),
            )
        conn.commit()
        return True, None
    except Exception as e:
        logger.warning("set_instrument_class failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False, "Failed to save the instrument class."


def delete_instrument_class(conn: Any, contract_key: str) -> bool:
    """Drop the registration; the instrument reads as a stock again."""
    ck = str(contract_key or "").strip()
    if not ck or conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM preference_instrument_class WHERE contract_key = %s", (ck,))
        conn.commit()
        return True
    except Exception as e:
        logger.debug("delete_instrument_class failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
