"""Short option legs read: what it selects, and what it refuses to decide."""

from __future__ import annotations

from typing import Any, Dict, List

from bifrost_core.portfolio.services.short_legs import get_short_option_legs


class _FakeCursor:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self.executed: List[tuple] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> List[Dict[str, Any]]:
        return self._rows

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self.cur = _FakeCursor(rows)

    def cursor(self, **_: Any) -> _FakeCursor:
        return self.cur


def _row(**over: Any) -> Dict[str, Any]:
    base = {
        "account_id": "U1",
        "symbol": "nvda",
        "expiry": "20261120",
        "strike": 180.0,
        "option_right": "c",
        "qty": -2,
        "contract_key": "NVDA_20261120_180_C",
        "stk_mid": 172.15,
        "stk_last": 172.0,
    }
    base.update(over)
    return base


def test_normalises_symbol_and_right_and_prefers_mid_over_last() -> None:
    legs = get_short_option_legs(_FakeConn([_row()]))
    assert len(legs) == 1
    leg = legs[0]
    assert leg["symbol"] == "NVDA"
    assert leg["right"] == "C"
    assert leg["qty"] == -2
    # Mid first, then last -- the same preference the model analysis uses.
    assert leg["spot"] == 172.15


def test_falls_back_to_last_and_reports_no_spot_as_null() -> None:
    assert get_short_option_legs(_FakeConn([_row(stk_mid=None)]))[0]["spot"] == 172.0
    # A naked short on a name whose stock carries no quote. Null, never a guess:
    # the caller counts it as unpriced rather than as safe.
    assert get_short_option_legs(_FakeConn([_row(stk_mid=None, stk_last=None)]))[0]["spot"] is None
    assert get_short_option_legs(_FakeConn([_row(stk_mid=0, stk_last=-1)]))[0]["spot"] is None


def test_filters_to_the_accounts_asked_for() -> None:
    conn = _FakeConn([])
    get_short_option_legs(conn, ["U1", "U2"])
    assert conn.cur.executed[0][1] == {"accounts": ["U1", "U2"]}
    conn = _FakeConn([])
    get_short_option_legs(conn)
    assert conn.cur.executed[0][1] == {"accounts": None}


def test_the_query_asks_only_for_short_option_legs() -> None:
    conn = _FakeConn([])
    get_short_option_legs(conn)
    sql = conn.cur.executed[0][0]
    assert "p.sec_type = 'OPT'" in sql
    assert "p.position < 0" in sql
    assert "q.sec_type = 'STK'" in sql


def test_returns_no_cushion_and_no_verdict() -> None:
    """The rule and the trader's warning line live in one place, and it is not here."""
    leg = get_short_option_legs(_FakeConn([_row()]))[0]
    assert set(leg) == {
        "account_id",
        "symbol",
        "expiry",
        "strike",
        "right",
        "qty",
        "contract_key",
        "spot",
    }
