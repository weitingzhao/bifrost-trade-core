"""``executions_fly`` (``source_scope=on_the_fly``) holds exactly the TWS fills Flex has not confirmed (core 0.61.0).

A TWS fill is pending while Flex has no row with its exec_id, its trading day (US/Eastern) is after
the last day Flex covers for its account, it has an account and it is not a BAG. Before 0.61.0 a
fill counted as confirmed when Flex had *any* row for the same account and contract (or, for a
stock, the same symbol): on 2026-10-16 a new NVDA option fill on a contract traded before was
missing from the view while IB's position had moved.

Marked ``db``; runs inside the fixture's transaction, rolled back. Accounts, symbols, ids and
numbers are made up.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, List, Optional

import pytest

from bifrost_core.persistence.postgres.brokerage_ddl import ensure_brokerage_schema

pytestmark = pytest.mark.db

A = "U0000071"  # has Flex through D2
B = "U0000072"  # has no Flex row at all
D1 = date(2026, 9, 18)
D2 = date(2026, 9, 19)
OPT_KEY = "ZZQ   261120C00245000|OPT|20261120|245.0|C"


def _ts(day: int, hour: int) -> datetime:
    return datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)


def _flex(cur: Any, exec_id: str, account: str, trade_date: date, ck: str = OPT_KEY, sec: str = "OPT",
          symbol: str = "ZZQ") -> None:
    cur.execute(
        "INSERT INTO raw_broker.executions_raw_flex (exec_id, account_id, symbol, sec_type, side, quantity, price, "
        "source, contract_key, trade_date, exec_time) VALUES (%s, %s, %s, %s, 'BUY', 1, 1.0, 'flex_trades', %s, %s, "
        "%s)",
        (exec_id, account, symbol, sec, ck, trade_date, datetime(trade_date.year, trade_date.month, trade_date.day,
                                                                 15, tzinfo=timezone.utc)),
    )


def _tws(cur: Any, exec_id: Optional[str], account: Optional[str], exec_time: Optional[datetime],
         ck: str = OPT_KEY, sec: str = "OPT", symbol: str = "ZZQ") -> int:
    cur.execute(
        "INSERT INTO raw_broker.executions_raw_tws (exec_id, account_id, symbol, sec_type, side, quantity, price, "
        "source, contract_key, exec_time) VALUES (%s, %s, %s, %s, 'BOT', 1, 1.0, 'tws_client', %s, %s) "
        "RETURNING executions_raw_tws_id",
        (exec_id, account, symbol, sec, ck, exec_time),
    )
    return int(cur.fetchone()[0])


def _fly(cur: Any) -> List[str]:
    cur.execute(
        "SELECT exec_id FROM raw_broker.executions_fly WHERE account_id IN (%s, %s) ORDER BY exec_id", (A, B)
    )
    return [r[0] for r in cur.fetchall()]


@pytest.fixture
def cur(pg_conn: Any) -> Any:
    ensure_brokerage_schema(pg_conn, log=lambda m: None)
    with pg_conn.cursor() as c:
        _flex(c, "f.d1", A, D1)
        _flex(c, "f.d2", A, D2)
        _flex(c, "f.stk", A, D2, ck="ZZS|STK|||", sec="STK", symbol="ZZS")
        yield c


def test_a_new_fill_on_a_contract_flex_already_has_is_pending(cur: Any) -> None:
    """The 2026-10-16 case: same account and contract as earlier Flex rows, new exec_id, later day."""
    _tws(cur, "t.new", A, _ts(20, 16))
    assert _fly(cur) == ["t.new"]


def test_a_fill_flex_has_by_exec_id_is_not_pending(cur: Any) -> None:
    _tws(cur, "f.d2", A, _ts(20, 16))
    assert _fly(cur) == []


def test_a_fill_on_or_before_the_last_flex_day_is_not_pending(cur: Any) -> None:
    """A TWS row Flex covers under another exec_id (older imports) must not be counted twice."""
    _tws(cur, "t.same_day", A, _ts(19, 16))
    _tws(cur, "t.older", A, _ts(18, 16))
    assert _fly(cur) == []


def test_the_trading_day_is_the_eastern_date(cur: Any) -> None:
    """02:00 UTC on 9/20 is 22:00 on 9/19 in New York: the last Flex day, so not pending."""
    _tws(cur, "t.late", A, _ts(20, 2))
    _tws(cur, "t.next", A, _ts(20, 14))
    assert _fly(cur) == ["t.next"]


def test_a_stock_with_a_flex_symbol_is_pending_when_the_fill_is_new(cur: Any) -> None:
    _tws(cur, "t.stk", A, _ts(20, 16), ck="ZZS|STK|||", sec="STK", symbol="ZZS")
    assert _fly(cur) == ["t.stk"]


def test_an_account_with_no_flex_rows_has_no_bound(cur: Any) -> None:
    _tws(cur, "t.b", B, _ts(1, 16))
    assert _fly(cur) == ["t.b"]


def test_no_account_and_bag_rows_are_never_pending(cur: Any) -> None:
    _tws(cur, "t.noacct", None, _ts(20, 16))
    _tws(cur, "t.bag", A, _ts(20, 16), ck="ZZQ|BAG|||", sec="BAG")
    cur.execute("SELECT count(*) FROM raw_broker.executions_fly WHERE exec_id IN ('t.noacct', 't.bag')")
    assert cur.fetchone()[0] == 0


def test_a_row_without_exec_time_uses_its_created_day(cur: Any) -> None:
    """created_at is now, which is after D2: pending."""
    _tws(cur, "t.notime", A, None)
    assert _fly(cur) == ["t.notime"]


def test_when_flex_arrives_the_fill_moves_to_the_book_once(cur: Any) -> None:
    tws_id = _tws(cur, "t.new", A, _ts(20, 16))
    assert _fly(cur) == ["t.new"]
    _flex(cur, "t.new", A, date(2026, 9, 20))
    assert _fly(cur) == []
    cur.execute("SELECT count(*) FROM raw_broker.executions_final WHERE exec_id = 't.new'")
    assert cur.fetchone()[0] == 1
    # the view over every fill keeps one row for it too (Flex's)
    cur.execute("SELECT account_executions_id FROM raw_broker.executions WHERE exec_id = 't.new'")
    ids = [r[0] for r in cur.fetchall()]
    assert len(ids) == 1 and ids[0] > 0 and -tws_id not in ids
