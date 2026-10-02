"""One rule for when a contract_quote_live row is still a price (debt TD-02)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

import bifrost_core.monitor.reader.market as market_module
from bifrost_core.portfolio.model.core import compute_model_analysis
from bifrost_core.portfolio.quote_freshness import (
    LIVE_QUOTE_MAX_AGE_SEC,
    fresh_quote_sql,
    quote_is_fresh,
    underlying_spot,
)
from bifrost_core.portfolio.reader.accounts_helpers import STK_LIVE_STALE_SEC

# Invented close (fixtures are never copied from DEV).
_CLOSE = (171.5, 1_790_000_000.0, 170.0)


@pytest.fixture
def closes(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    asked: List[str] = []

    def fake(_conn: Any, symbol: str) -> Optional[Tuple[float, float, Optional[float]]]:
        asked.append(symbol)
        return _CLOSE if symbol == "ZZQ" else None

    monkeypatch.setattr(market_module, "get_stock_day_fallback_price", fake)
    return asked


def test_one_threshold_for_every_reader() -> None:
    # The Positions page's STK gate and this rule are the same number.
    assert STK_LIVE_STALE_SEC == LIVE_QUOTE_MAX_AGE_SEC


def test_freshness_by_age_and_no_timestamp_is_stale() -> None:
    now = time.time()
    assert quote_is_fresh(now - 60, now=now)
    assert not quote_is_fresh(now - LIVE_QUOTE_MAX_AGE_SEC - 1, now=now)
    assert quote_is_fresh(datetime.fromtimestamp(now - 60, tz=timezone.utc), now=now)
    assert not quote_is_fresh(None)
    assert not quote_is_fresh("not a time")


def test_sql_condition_is_scoped_to_the_alias() -> None:
    cond = fresh_quote_sql("cq")
    assert cond.startswith("cq.updated_at >= now() - make_interval(secs => ")
    assert str(int(LIVE_QUOTE_MAX_AGE_SEC)) in cond


def test_underlying_spot_prefers_a_fresh_quote_then_the_close(closes: List[str]) -> None:
    now = time.time()
    assert underlying_spot(None, "ZZQ", mid=10.0, last=9.0, updated_at=now) == (10.0, "live", now)
    stale = now - LIVE_QUOTE_MAX_AGE_SEC - 60
    assert underlying_spot(None, "zzq", mid=10.0, updated_at=stale) == (171.5, "close", 1_790_000_000.0)
    assert underlying_spot(None, "NOPE") == (None, None, None)
    assert closes == ["ZZQ", "NOPE"]


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.sql.append(sql)

    def fetchall(self) -> List[Dict[str, Any]]:
        return self.conn.rows

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return {"net_liquidation": 100000.0, "total_cash": 50000.0, "buying_power": 80000.0}

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self.rows = rows
        self.sql: List[str] = []

    def cursor(self, **_: Any) -> _Cur:
        return _Cur(self)


def test_model_reads_only_fresh_quotes_and_measures_against_the_close(closes: List[str]) -> None:
    # The join drops stale rows, so a name whose only quote is old arrives with no price.
    stk = {
        "symbol": "ZZQ", "sec_type": "STK", "position": 100, "avg_cost": 150.0,
        "expiry": None, "strike": None, "option_right": None, "contract_key": "ZZQ",
        "price_mid": None, "price_last": None,
    }
    conn = _Conn([stk])
    out = compute_model_analysis(conn, "U0")
    assert fresh_quote_sql("cq") in conn.sql[0]
    entry = next(e for e in out["per_underlying"] if e["symbol"] == "ZZQ")
    assert (entry["spot"], entry["spot_source"]) == (171.5, "close")
