"""Position prices on the accounts read come from the daily close or nowhere (TD-260).

``contract_quote_live`` has had no writer since TD-240 and its rows are from March; joining it
served those March prices (and P&L computed from them) on /status. A stock now carries its last
daily close from the market-data plugin, dated by the bar. An option, and a stock on the light
path or with no close, carries no ``price`` and no ``unrealized_pnl``: absent, never 0.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

import bifrost_core.monitor.reader.market as market_module
from bifrost_core.portfolio.reader import accounts as accounts_reader

# Invented book and close (fixtures are never copied from DEV).
_CLOSES: Dict[str, Tuple[float, float, Optional[float]]] = {"ZZQ": (41.0, 1_790_000_000.0, 40.0)}
_POSITIONS: List[Dict[str, Any]] = [
    {"account_id": "U0000001", "symbol": "ZZQ", "sec_type": "STK", "position": 100, "avg_cost": 30.0,
     "contract_key": "ZZQ|STK|||", "expiry": None, "strike": None, "option_right": None},
    {"account_id": "U0000001", "symbol": "ZZR", "sec_type": "STK", "position": 50, "avg_cost": 12.0,
     "contract_key": "ZZR|STK|||", "expiry": None, "strike": None, "option_right": None},
    {"account_id": "U0000001", "symbol": "ZZQ", "sec_type": "OPT", "position": -2, "avg_cost": 150.0,
     "contract_key": "ZZQ|OPT|20311121|40.0|P", "expiry": "20311121", "strike": 40.0, "option_right": "P"},
]


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._last = ""

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = sql
        self._conn.sql.append(sql)

    def fetchone(self) -> Tuple[bool]:
        return (False,)

    def fetchall(self) -> List[Dict[str, Any]]:
        if "summary_extra" in self._last:
            return [{"account_id": "U0000001", "updated_at": None, "net_liquidation": None,
                     "total_cash": None, "buying_power": None, "summary_extra": None}]
        if "ap.avg_cost" in self._last:
            return [dict(p) for p in _POSITIONS]
        return []


class _Conn:
    def __init__(self) -> None:
        self.sql: List[str] = []

    def cursor(self, **_: Any) -> _Cur:
        return _Cur(self)


@pytest.fixture(autouse=True)
def _plugin_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market_module, "get_stock_day_fallback_price", lambda _c, s: _CLOSES.get(s))


def _by_key(out: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {p["contract_key"]: p for p in out[0]["positions"]}


def test_the_read_does_not_join_contract_quote_live() -> None:
    conn = _Conn()
    accounts_reader.get_accounts_from_tables(conn)
    assert not any("contract_quote_live" in s for s in conn.sql)


def test_a_stock_is_priced_at_its_dated_close() -> None:
    pos = _by_key(accounts_reader.get_accounts_from_tables(_Conn()))["ZZQ|STK|||"]
    assert (pos["price"], pos["price_updated_at"]) == (41.0, 1_790_000_000.0)
    assert pos["unrealized_pnl"] == pytest.approx((41.0 - 30.0) * 100)


def test_no_close_and_options_are_not_served_rather_than_zero() -> None:
    rows = _by_key(accounts_reader.get_accounts_from_tables(_Conn()))
    for key in ("ZZR|STK|||", "ZZQ|OPT|20311121|40.0|P"):
        assert "price" not in rows[key]
        assert "price_updated_at" not in rows[key]
        assert "unrealized_pnl" not in rows[key]


def test_the_light_path_serves_no_price_at_all() -> None:
    rows = _by_key(accounts_reader.get_accounts_from_tables(_Conn(), include_position_exec_times=False))
    assert all("price" not in p and "unrealized_pnl" not in p for p in rows.values())
