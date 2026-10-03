"""Opaque keyset cursors for the paged list readers (TD-51, core 0.40.0).

A cursor names the last row of a page by its sort key, so the next page starts strictly
after that row whatever was inserted or deleted meanwhile -- no offset, no row read twice
or skipped. It is urlsafe base64 (no padding) of a small JSON object; callers pass it back
unchanged and must not build or read it. :func:`decode` validates every field and raises
:class:`InvalidCursor` (a ``ValueError``) for anything it did not write: the API answers
that with 400.

Two kinds, each with the full sort key of its reader:

- ``executions`` -- ``(trade_date DESC NULLS LAST, exec_time DESC NULLS LAST,
  account_executions_id DESC)``. ``account_executions_id`` is unique in every execution
  view (sign-encoded per raw table), so the key is total. ``d`` / ``t`` may be null.
- ``transactions`` -- ``(ts DESC, account_transactions_id DESC)``; ``ts`` is NOT NULL.

Timestamps travel as ISO 8601 with offset and microseconds, the precision PostgreSQL
stores, so the comparison is exact (an epoch float is not).
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

VERSION = 1
EXECUTIONS = "executions"
TRANSACTIONS = "transactions"
_KINDS = (EXECUTIONS, TRANSACTIONS)
_MAX_TOKEN_LEN = 512


class InvalidCursor(ValueError):
    """The cursor was not written by :func:`encode` for this list."""


def encode(kind: str, payload: Dict[str, Any]) -> str:
    if kind not in _KINDS:
        raise ValueError(f"unknown cursor kind {kind!r}")
    body = {"v": VERSION, "k": kind, **payload}
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_body(token: Any, kind: str) -> Dict[str, Any]:
    if not isinstance(token, str) or not token.strip():
        raise InvalidCursor("cursor is empty")
    token = token.strip()
    if len(token) > _MAX_TOKEN_LEN:
        raise InvalidCursor("cursor is too long")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        body = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise InvalidCursor("cursor is not one this API issued") from None
    if not isinstance(body, dict) or body.get("v") != VERSION:
        raise InvalidCursor("cursor is not one this API issued")
    if body.get("k") != kind:
        raise InvalidCursor(f"cursor belongs to another list ({body.get('k')!r}), not {kind}")
    return body


def _id(body: Dict[str, Any]) -> int:
    v = body.get("i")
    if isinstance(v, bool) or not isinstance(v, int):
        raise InvalidCursor("cursor id is not an integer")
    return v


def _opt_date(body: Dict[str, Any], key: str) -> Optional[date]:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise InvalidCursor("cursor date is not a string")
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise InvalidCursor("cursor date is not YYYY-MM-DD") from None


def _opt_ts(body: Dict[str, Any], key: str) -> Optional[datetime]:
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise InvalidCursor("cursor timestamp is not a string")
    try:
        ts = datetime.fromisoformat(v)
    except ValueError:
        raise InvalidCursor("cursor timestamp is not ISO 8601") from None
    if ts.tzinfo is None:
        raise InvalidCursor("cursor timestamp has no offset")
    return ts


def _iso(v: Any) -> Optional[str]:
    return None if v is None else v.isoformat()


# --- executions -------------------------------------------------------------------------


def encode_executions(trade_date: Optional[date], exec_time: Optional[datetime], account_executions_id: int) -> str:
    return encode(EXECUTIONS, {"d": _iso(trade_date), "t": _iso(exec_time), "i": int(account_executions_id)})


def decode_executions(token: Any) -> Tuple[Optional[date], Optional[datetime], int]:
    body = _decode_body(token, EXECUTIONS)
    if set(body) != {"v", "k", "d", "t", "i"}:
        raise InvalidCursor("cursor fields do not match the executions list")
    return _opt_date(body, "d"), _opt_ts(body, "t"), _id(body)


def executions_after_sql(
    key: Tuple[Optional[date], Optional[datetime], int], alias: str = "e"
) -> Tuple[str, List[Any]]:
    """WHERE fragment: rows strictly after ``key`` in ``trade_date DESC NULLS LAST,
    exec_time DESC NULLS LAST, account_executions_id DESC``.

    A NULL cursor value sits in the last segment of its column: nothing comes after it on
    that column, only its ties (``IS NULL``) continue to the next column. Written with
    plain comparisons and ``OR`` (no row constructor), which postgres_fdw can ship.
    """
    d, t, i = key
    p = f"{alias}." if alias else ""
    params: List[Any] = []

    # innermost: same trade_date and exec_time, lower id
    if t is None:
        time_part = f"({p}exec_time IS NULL AND {p}account_executions_id < %s)"
        time_params: List[Any] = [i]
    else:
        time_part = (
            f"({p}exec_time < %s OR {p}exec_time IS NULL "
            f"OR ({p}exec_time = %s AND {p}account_executions_id < %s))"
        )
        time_params = [t, t, i]

    if d is None:
        sql = f"({p}trade_date IS NULL AND {time_part})"
        params.extend(time_params)
    else:
        sql = f"({p}trade_date < %s OR {p}trade_date IS NULL OR ({p}trade_date = %s AND {time_part}))"
        params.extend([d, d])
        params.extend(time_params)
    return sql, params


# --- transactions -----------------------------------------------------------------------


def encode_transactions(ts: datetime, account_transactions_id: int) -> str:
    return encode(TRANSACTIONS, {"t": _iso(ts), "i": int(account_transactions_id)})


def decode_transactions(token: Any) -> Tuple[datetime, int]:
    body = _decode_body(token, TRANSACTIONS)
    if set(body) != {"v", "k", "t", "i"}:
        raise InvalidCursor("cursor fields do not match the transactions list")
    ts = _opt_ts(body, "t")
    if ts is None:
        raise InvalidCursor("cursor timestamp is missing")
    return ts, _id(body)


def transactions_after_sql(key: Tuple[datetime, int]) -> Tuple[str, List[Any]]:
    """Rows strictly after ``key`` in ``ts DESC, account_transactions_id DESC`` (ts NOT NULL).

    The leading ``ts <= %s`` is implied by the rest; it is there so Golden Source can read
    ``(account_id, ts DESC)`` as a range instead of filtering the whole account.
    """
    ts, i = key
    return "(ts <= %s AND (ts < %s OR (ts = %s AND account_transactions_id < %s)))", [ts, ts, ts, i]
