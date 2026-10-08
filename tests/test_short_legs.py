"""Short option legs read: what it selects, and what it refuses to decide."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

import bifrost_core.monitor.reader.market as market_module
from bifrost_core.portfolio.services.short_legs import get_short_option_legs

# Invented closes (fixtures are never copied from DEV).
_CLOSES: Dict[str, Tuple[float, float, Optional[float]]] = {"NVDA": (171.5, 1_790_000_000.0, 170.0)}


@pytest.fixture(autouse=True)
def _plugin_closes(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """The last-close fallback reads the market-data plugin; tests answer from _CLOSES."""
    asked: List[str] = []

    def fake(_conn: Any, symbol: str) -> Optional[Tuple[float, float, Optional[float]]]:
        asked.append(symbol)
        return _CLOSES.get(symbol)

    monkeypatch.setattr(market_module, "get_stock_day_fallback_price", fake)
    return asked


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
    }
    base.update(over)
    return base


def test_normalises_symbol_and_right() -> None:
    legs = get_short_option_legs(_FakeConn([_row()]))
    assert len(legs) == 1
    leg = legs[0]
    assert leg["symbol"] == "NVDA"
    assert leg["right"] == "C"
    assert leg["qty"] == -2


def test_spot_is_the_last_close_and_no_close_is_null() -> None:
    """TD-260: contract_quote_live has no writer, so the read never takes a live quote from it."""
    leg = get_short_option_legs(_FakeConn([_row()]))[0]
    assert (leg["spot"], leg["spot_source"], leg["spot_as_of"]) == (171.5, "close", 1_790_000_000.0)
    # A name with no close. Null, never a guess: the caller counts it as unpriced rather than as safe.
    leg = get_short_option_legs(_FakeConn([_row(symbol="zzzz")]))[0]
    assert (leg["spot"], leg["spot_source"], leg["spot_as_of"]) == (None, None, None)


def test_the_close_is_read_once_per_symbol(_plugin_closes: List[str]) -> None:
    rows = [_row(strike=180.0), _row(strike=200.0)]
    legs = get_short_option_legs(_FakeConn(rows))
    assert [leg["spot"] for leg in legs] == [171.5, 171.5]
    assert _plugin_closes == ["NVDA"]


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
    assert "contract_quote_live" not in sql


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
        "spot_source",
        "spot_as_of",
    }
