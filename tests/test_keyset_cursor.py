"""Keyset cursors for /executions and /transactions (TD-51, core 0.40.0): encoding,
validation, the WHERE fragments and the page readers' limit + 1 probe.

The real-Postgres half (ties, NULLs, every scope, no row skipped or repeated) is
``test_keyset_pages_db.py``. Values are made up.
"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from bifrost_core.portfolio.reader import executions as ex
from bifrost_core.portfolio.reader import keyset
from bifrost_core.portfolio.reader.keyset import InvalidCursor

TS = datetime(2026, 9, 18, 15, 0, 0, 1, tzinfo=timezone.utc)


def _raw(body: Dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")


# --- encode / decode ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        (date(2026, 9, 18), TS, 41),
        (date(2026, 9, 18), None, -7),
        (None, TS, -1_000_000_003),
        (None, None, 5),
        (date(2026, 9, 18), datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=-5))), 1),
    ],
)
def test_executions_round_trip(key: tuple) -> None:
    token = keyset.encode_executions(*key)
    assert "=" not in token and "+" not in token and "/" not in token
    assert keyset.decode_executions(token) == key


def test_microseconds_survive() -> None:
    d, t, i = keyset.decode_executions(keyset.encode_executions(None, TS, 1))
    assert t == TS and t.microsecond == 1


def test_transactions_round_trip() -> None:
    assert keyset.decode_transactions(keyset.encode_transactions(TS, 9)) == (TS, 9)


@pytest.mark.parametrize(
    "token, says",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("not base64 !!", "not one this API issued"),
        (base64.urlsafe_b64encode(b"\xff\xfe").decode(), "not one this API issued"),
        (_raw([1, 2]), "not one this API issued"),
        (_raw({"v": 2, "k": "executions", "d": None, "t": None, "i": 1}), "not one this API issued"),
        (_raw({"v": 1, "k": "transactions", "t": TS.isoformat(), "i": 1}), "another list"),
        (_raw({"v": 1, "k": "executions", "d": None, "t": None}), "fields"),
        (_raw({"v": 1, "k": "executions", "d": None, "t": None, "i": 1, "x": 0}), "fields"),
        (_raw({"v": 1, "k": "executions", "d": None, "t": None, "i": "1"}), "integer"),
        (_raw({"v": 1, "k": "executions", "d": None, "t": None, "i": True}), "integer"),
        (_raw({"v": 1, "k": "executions", "d": "2026-13-01", "t": None, "i": 1}), "YYYY-MM-DD"),
        (_raw({"v": 1, "k": "executions", "d": 20260918, "t": None, "i": 1}), "not a string"),
        (_raw({"v": 1, "k": "executions", "d": None, "t": "2026-09-18T15:00:00", "i": 1}), "offset"),
        (_raw({"v": 1, "k": "executions", "d": None, "t": "yesterday", "i": 1}), "ISO 8601"),
        ("A" * 600, "too long"),
    ],
)
def test_bad_executions_cursors(token: str, says: str) -> None:
    with pytest.raises(InvalidCursor, match=says):
        keyset.decode_executions(token)


def test_transactions_cursor_needs_a_timestamp() -> None:
    with pytest.raises(InvalidCursor, match="missing"):
        keyset.decode_transactions(_raw({"v": 1, "k": "transactions", "t": None, "i": 1}))
    with pytest.raises(InvalidCursor, match="another list"):
        keyset.decode_transactions(keyset.encode_executions(None, None, 1))


def test_invalid_cursor_is_a_value_error() -> None:
    assert issubclass(InvalidCursor, ValueError)


# --- WHERE fragments --------------------------------------------------------------------


def test_after_sql_full_key() -> None:
    d = date(2026, 9, 18)
    sql, params = keyset.executions_after_sql((d, TS, 7))
    assert sql == (
        "(e.trade_date < %s OR e.trade_date IS NULL OR (e.trade_date = %s AND "
        "(e.exec_time < %s OR e.exec_time IS NULL OR (e.exec_time = %s AND e.account_executions_id < %s))))"
    )
    assert params == [d, d, TS, TS, 7]


def test_after_sql_null_segments() -> None:
    sql, params = keyset.executions_after_sql((None, None, 7))
    assert sql == "(e.trade_date IS NULL AND (e.exec_time IS NULL AND e.account_executions_id < %s))"
    assert params == [7]
    sql, params = keyset.executions_after_sql((date(2026, 9, 18), None, 7))
    assert "e.exec_time IS NULL AND e.account_executions_id < %s" in sql
    assert params == [date(2026, 9, 18), date(2026, 9, 18), 7]
    sql, params = keyset.executions_after_sql((None, TS, 7))
    assert sql.startswith("(e.trade_date IS NULL AND (e.exec_time < %s")
    assert params == [TS, TS, 7]


def test_no_row_constructor() -> None:
    """postgres_fdw does not ship a row comparison; the fragments use plain operators."""
    for sql, _ in (keyset.executions_after_sql((date(2026, 9, 18), TS, 7)), keyset.transactions_after_sql((TS, 7))):
        assert "ROW(" not in sql.upper() and ") <" not in sql


def _sort_key(row: Dict[str, Any]) -> tuple:
    """Python twin of ORDER BY trade_date DESC NULLS LAST, exec_time DESC NULLS LAST, id DESC."""
    d, t, i = row["d"], row["t"], row["i"]
    return (
        d is None,
        -(d.toordinal()) if d else 0,
        t is None,
        -(t.timestamp()) if t else 0,
        -i,
    )


def _after(row: Dict[str, Any], key: tuple) -> bool:
    """The fragment's logic evaluated in Python with SQL NULL semantics (None = unknown)."""
    d, t, i = key

    def lt(a: Any, b: Any) -> Optional[bool]:
        return None if a is None or b is None else a < b

    def eq(a: Any, b: Any) -> Optional[bool]:
        return None if a is None or b is None else a == b

    def or_(*xs: Optional[bool]) -> Optional[bool]:
        return True if any(x is True for x in xs) else (None if any(x is None for x in xs) else False)

    def and_(*xs: Optional[bool]) -> Optional[bool]:
        return False if any(x is False for x in xs) else (None if any(x is None for x in xs) else True)

    rd, rt, ri = row["d"], row["t"], row["i"]
    if t is None:
        time_part = and_(rt is None, ri < i)
    else:
        time_part = or_(lt(rt, t), rt is None, and_(eq(rt, t), ri < i))
    if d is None:
        res = and_(rd is None, time_part)
    else:
        res = or_(lt(rd, d), rd is None, and_(eq(rd, d), time_part))
    return res is True


def test_after_logic_matches_the_order_for_every_pair() -> None:
    """For every row as cursor, the rows the fragment keeps are exactly those that sort after it."""
    dates = (date(2026, 9, 19), date(2026, 9, 18), None)
    times = (TS + timedelta(microseconds=1), TS, None)
    rows: List[Dict[str, Any]] = []
    n = 0
    for d in dates:
        for t in times:
            for _ in range(2):
                n += 1
                rows.append({"d": d, "t": t, "i": n if n % 2 else -n})
    ordered = sorted(rows, key=_sort_key)
    for pos, cur in enumerate(ordered):
        key = (cur["d"], cur["t"], cur["i"])
        kept = [r for r in ordered if _after(r, key)]
        assert kept == ordered[pos + 1 :], key


# --- page readers over a fake connection ------------------------------------------------


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, q: str, args: List[Any]) -> None:
        self.conn.sql.append((q, list(args)))

    def fetchall(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.rows]


class _Conn:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self.rows = rows
        self.sql: List[tuple] = []

    def cursor(self, cursor_factory: Any = None) -> _Cursor:
        return _Cursor(self)


def _txn(i: int, ts: datetime) -> Dict[str, Any]:
    return {"account_transactions_id": i, "account_id": "U0000001", "ts": ts.timestamp(), "amount": 1.0,
            "type": "Deposits", "currency": "USD", "description": None, "created_at": None, "symbol": None,
            "conid": None, ex._TXN_KEY_COL: ts}


def test_transactions_page_reads_one_more_and_cuts() -> None:
    conn = _Conn([_txn(3, TS), _txn(2, TS), _txn(1, TS - timedelta(seconds=1))])
    page = ex.get_transactions_page(conn, account_id="U0000001", limit=2)
    q, args = conn.sql[0]
    assert args == ["U0000001", 3]
    assert "ORDER BY _keyset_ts DESC, account_transactions_id DESC LIMIT %s" in q
    assert [r["account_transactions_id"] for r in page["items"]] == [3, 2]
    assert all(ex._TXN_KEY_COL not in r for r in page["items"])
    assert keyset.decode_transactions(page["next_cursor"]) == (TS, 2)


def test_transactions_page_with_cursor_adds_the_predicate() -> None:
    conn = _Conn([_txn(1, TS)])
    page = ex.get_transactions_page(conn, limit=5, cursor=keyset.encode_transactions(TS, 2))
    q, args = conn.sql[0]
    assert "(ts <= %s AND (ts < %s OR (ts = %s AND account_transactions_id < %s)))" in q
    assert args == [TS, TS, TS, 2, 6]
    assert page["next_cursor"] is None and len(page["items"]) == 1


def test_transactions_page_bad_cursor_raises_before_reading() -> None:
    conn = _Conn([])
    with pytest.raises(InvalidCursor):
        ex.get_transactions_page(conn, cursor="garbage")
    assert conn.sql == []


def test_get_transactions_rows_carry_no_key_column() -> None:
    conn = _Conn([_txn(1, TS)])
    rows = ex.get_transactions(conn, limit=5)
    assert ex._TXN_KEY_COL not in rows[0]
    assert conn.sql[0][1] == [5]


def _exec(i: int, d: Optional[date], t: Optional[datetime]) -> Dict[str, Any]:
    return {"account_executions_id": i, "trade_date": d, "time": t.timestamp() if t else None,
            "raw_extra": None, "created_at": None, "sec_type": "STK", ex._EXEC_KEY_COL: t}


def test_executions_page_cuts_and_encodes_the_exact_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "attach_instance_allocations", lambda conn, rows: None)
    d = date(2026, 9, 18)
    conn = _Conn([_exec(5, d, TS), _exec(4, d, TS), _exec(-3, None, None)])
    page = ex.get_executions_page(conn, account_id="U0000001", limit=2)
    q, args = conn.sql[0]
    assert args[-1] == 3
    assert [r["account_executions_id"] for r in page["items"]] == [5, 4]
    assert all(ex._EXEC_KEY_COL not in r for r in page["items"])
    assert keyset.decode_executions(page["next_cursor"]) == (d, TS, 4)


def test_executions_page_without_limit_has_no_next(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "attach_instance_allocations", lambda conn, rows: None)
    conn = _Conn([_exec(5, None, None)])
    page = ex.get_executions_page(conn, limit=0, cursor=keyset.encode_executions(None, None, 9))
    q, args = conn.sql[0]
    assert "LIMIT" not in q.split("ORDER BY")[-1]
    assert args == [9]
    assert page == {"items": [{k: v for k, v in _exec(5, None, None).items() if k != ex._EXEC_KEY_COL}], "next_cursor": None}


def test_get_executions_rows_carry_no_key_column(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ex, "attach_instance_allocations", lambda conn, rows: None)
    rows = ex.get_executions(_Conn([_exec(5, None, TS)]), limit=1)
    assert ex._EXEC_KEY_COL not in rows[0]


def test_executions_page_bad_cursor_raises_before_reading() -> None:
    conn = _Conn([])
    with pytest.raises(InvalidCursor):
        ex.get_executions_page(conn, cursor=keyset.encode_transactions(TS, 1))
    assert conn.sql == []
