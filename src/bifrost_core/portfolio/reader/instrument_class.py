"""Instrument class: what kind of security a stock-like holding is (preference_instrument_class).

IB books a bond or T-bill ETF as STK, and neither the broker nor the data vendor
says which funds are fixed income or cash-like, so the Owner registers it once
per instrument (trade design Rev .119, Owner-approved 2026-09-30). It is a
property of the security, not of an account: one row per `contract_key`, the
same key positions, watchlist and category tags use (STK: `SYMBOL|STK|||`).

An instrument with no row is read as a stock by the callers; nothing here
infers a class from the Owner's category.

``patch_instrument_class`` / ``delete_instrument_class_strict`` (core 0.33.0, TD-15)
raise ``Write*``; so does ``set_instrument_class_strict`` (core 0.47.0, TD-80 C2), the full
replace the API's PUT calls. ``set_instrument_class`` (``(ok, error)``) stays one release for
the ``StatusReader`` facade, then goes.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteInvalid, WriteNotFound

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
    *,
    keep_note: bool = True,
) -> Tuple[bool, Optional[str]]:
    """Register or change one instrument's class. Returns (ok, error_message).

    ``keep_note`` (the default) keeps a stored note when none is sent. ``False`` is a
    full replace: the row becomes exactly what was sent, so no note clears it (TD-15,
    PUT /instrument-classes since api 0.6.0)."""
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
                    note = CASE WHEN %s THEN COALESCE(EXCLUDED.note, preference_instrument_class.note)
                                ELSE EXCLUDED.note END,
                    updated_at = now()
                """,
                (ck, cls, (note or "").strip() or None, bool(keep_note)),
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


# --- TD-15 writers (core 0.33.0): return the row / raise Write* ---------------------

INSTRUMENT_CLASS_PATCHABLE = ("instrument_class", "note")
_CLASS_COLUMNS = "contract_key, instrument_class, note, created_at, updated_at"


def _contract_key(contract_key: Any) -> str:
    ck = str(contract_key or "").strip()
    if not ck:
        raise WriteInvalid("contract_key is required.")
    return ck


def patch_instrument_class(conn_or_config: Any, contract_key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change a registered instrument's class or note; return the row in ``list_instrument_classes``' shape.

    Does not insert: an unregistered ``contract_key`` is WriteNotFound (``set_instrument_class``
    registers). ``instrument_class`` NOT NULL, one of stock / fixed_income / cash_like
    (spelling normalised as ``normalize_instrument_class`` does) · ``note`` nullable text
    (null clears -- the upsert cannot). Raises WriteInvalid, WriteNotFound, WriteFailed.
    """
    ck = _contract_key(contract_key)
    what = f"the instrument class of {ck}"
    fields = ws.check_fields(fields, INSTRUMENT_CLASS_PATCHABLE, "instrument class")
    columns: Dict[str, Any] = {}
    if "instrument_class" in fields:
        raw = ws.text(fields["instrument_class"], "instrument_class", nullable=False)
        cls = normalize_instrument_class(raw)
        if cls is None:
            raise WriteInvalid(f"instrument_class must be one of {', '.join(INSTRUMENT_CLASSES)}.")
        columns["instrument_class"] = cls
    if "note" in fields:
        columns["note"] = ws.text(fields["note"], "note", nullable=True)
    assignments, values = ws.set_clause(columns)
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"UPDATE preference_instrument_class SET {assignments} WHERE contract_key = %s RETURNING {_CLASS_COLUMNS}",
                [*values, ck],
            )
            row = cur.fetchone()
        if row is None:
            raise WriteNotFound(f"{ck} has no instrument class registered.")
    return dict(row)


def set_instrument_class_strict(
    conn_or_config: Any,
    contract_key: Any,
    instrument_class: Any,
    note: Any = None,
) -> Dict[str, Any]:
    """Register one instrument's class, or replace its registration whole; return the row in
    ``list_instrument_classes``' shape.

    A full replace: the row becomes exactly what is sent, so no ``note`` clears a stored one
    (PATCH changes only the fields sent). ``instrument_class`` NOT NULL, one of stock /
    fixed_income / cash_like (spelling normalised as ``normalize_instrument_class`` does) ·
    ``note`` nullable text, blank refused. Raises WriteInvalid, WriteFailed.
    """
    ck = _contract_key(contract_key)
    raw = ws.text(instrument_class, "instrument_class", nullable=False)
    cls = normalize_instrument_class(raw)
    if cls is None:
        raise WriteInvalid(f"instrument_class must be one of {', '.join(INSTRUMENT_CLASSES)}.")
    note_text = ws.text(note, "note", nullable=True)
    what = f"the instrument class of {ck}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                INSERT INTO preference_instrument_class (contract_key, instrument_class, note, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (contract_key) DO UPDATE
                SET instrument_class = EXCLUDED.instrument_class, note = EXCLUDED.note, updated_at = now()
                RETURNING {_CLASS_COLUMNS}
                """,
                (ck, cls, note_text),
            )
            row = cur.fetchone()
    return dict(row)


def delete_instrument_class_strict(conn_or_config: Any, contract_key: str) -> Dict[str, Any]:
    """Drop the registration. Returns ``{"deleted": "hard", "contract_key"}``; WriteNotFound when
    none was registered, WriteFailed on a DB failure."""
    ck = _contract_key(contract_key)
    what = f"the instrument class of {ck}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM preference_instrument_class WHERE contract_key = %s", (ck,))
            if cur.rowcount == 0:
                raise WriteNotFound(f"{ck} has no instrument class registered.")
    return {"deleted": "hard", "contract_key": ck}
