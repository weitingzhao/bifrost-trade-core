"""HTTP read client for Plugin Market Data API (bars + coverage).

Used by monitor/reader/market.py READ functions to GET bars data
via the Plugin API instead of direct psycopg2 SQL.

Pattern mirrors market_write_client.py (urllib only, no new deps).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _plugin_base_url() -> str:
    return os.environ.get("MARKET_DATA_PLUGIN_URL", "http://localhost:8790/market")


def _get_json(path: str, params: Optional[Dict[str, str]] = None, timeout: int = 30) -> Dict[str, Any]:
    """GET JSON from Plugin API."""
    base = _plugin_base_url()
    url = f"{base}{path}"
    if params:
        qs = "&".join(
            f"{k}={urllib.parse.quote(str(v))}"
            for k, v in params.items()
            if v is not None
        )
        url = f"{url}?{qs}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def get_bars_via_plugin(
    symbol: str,
    period: str = "1 D",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """GET /stocks/db/bars → {ok, rows}. Returns rows list."""
    resp = _get_json(
        "/stocks/db/bars",
        {"symbol": symbol, "period": period, "limit": str(limit)},
    )
    return resp.get("rows", [])


def get_bars_latest_via_plugin(
    symbol: str,
    period: str = "1 D",
) -> Optional[float]:
    """GET /stocks/db/bars/latest → {ok, latest_ts}. Returns Unix timestamp or None."""
    resp = _get_json(
        "/stocks/db/bars/latest",
        {"symbol": symbol, "period": period},
    )
    ts = resp.get("latest_ts")
    return float(ts) if ts is not None else None


def get_bar_times_in_range_via_plugin(
    symbol: str,
    period: str = "1 D",
    start_ts: float = 0,
    end_ts: float = 0,
) -> List[float]:
    """GET /stocks/db/bars/range → {ok, times}. Returns list of Unix timestamps."""
    resp = _get_json(
        "/stocks/db/bars/range",
        {
            "symbol": symbol,
            "period": period,
            "start_ts": str(start_ts),
            "end_ts": str(end_ts),
        },
    )
    return [float(t) for t in (resp.get("times") or []) if t is not None]


def get_bars_benchmark_via_plugin(
    symbols: List[str],
    on_or_before: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """GET /stocks/db/bars/benchmark → {ok, data}. Returns {symbol: {bar_time, close, prev_close}}."""
    params: Dict[str, str] = {"symbols": ",".join(symbols)}
    if on_or_before:
        params["on_or_before"] = on_or_before
    resp = _get_json("/stocks/db/bars/benchmark", params)
    return resp.get("data", {})


def get_fallback_price_via_plugin(
    symbol: str,
) -> Dict[str, Any]:
    """GET /stocks/db/bars/fallback-price → {ok, symbol, found, close, bar_time, prev_close}."""
    return _get_json("/stocks/db/bars/fallback-price", {"symbol": symbol})


def get_bars_stats_via_plugin(
    symbol: str,
) -> Dict[str, Any]:
    """GET /stocks/db/bars/stats → {ok, symbol, stock_day, stock_min}."""
    return _get_json("/stocks/db/bars/stats", {"symbol": symbol})


def get_bars_coverage_via_plugin(
    symbols: List[str],
) -> List[Dict[str, Any]]:
    """GET /stocks/db/bars/coverage → {ok, symbols, count}. Returns list of coverage dicts."""
    resp = _get_json("/stocks/db/bars/coverage", {"symbols": ",".join(symbols)})
    return resp.get("symbols", [])


def get_caret_symbols_via_plugin() -> List[str]:
    """GET /stocks/db/bars/caret-symbols → {ok, symbols}. Returns list of ^symbols."""
    resp = _get_json("/stocks/db/bars/caret-symbols")
    return resp.get("symbols", [])


# ─── Option bars endpoints (W2 cleanup) ───────────────────────────────────────


def get_option_bars_daily_via_plugin(
    underlying: str,
    expiry: str,
    strike: float,
    option_right: str,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """GET /options/daily → rows with bar_date, open, high, low, close, volume."""
    params: Dict[str, str] = {
        "symbol": underlying,
        "expiry": expiry,
        "days": "3650",
        "limit": str(min(limit, 500)),
    }
    resp = _get_json("/options/daily", params, timeout=30)
    rows = resp.get("rows", [])
    filtered: List[Dict[str, Any]] = []
    exp_str = expiry[:10] if len(expiry) >= 10 else expiry
    for r in rows:
        r_exp = str(r.get("expiry") or "")[:10]
        r_strike = float(r.get("strike") or 0)
        r_right = str(r.get("option_right") or "").strip().upper()
        if r_exp == exp_str and abs(r_strike - strike) < 0.001 and r_right == option_right.upper():
            filtered.append(r)
    return filtered


def get_option_bars_minute_via_plugin(
    underlying: str,
    expiry: str,
    strike: float,
    option_right: str,
    period: str = "1 minute",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """GET /options/minute → rows with time (epoch), open, high, low, close, volume, vwap."""
    params: Dict[str, str] = {
        "underlying": underlying,
        "expiry": expiry,
        "strike": str(strike),
        "option_right": option_right,
        "period": period,
        "limit": str(min(limit, 500)),
    }
    resp = _get_json("/options/minute", params, timeout=30)
    return resp.get("rows", [])


# ─── Readiness data endpoints (W2 cleanup) ────────────────────────────────────


def get_readiness_bar_aggregate_via_plugin(
    window_days: int = 420,
) -> Dict[str, Dict[str, Any]]:
    """GET /readiness/bar-aggregate → {ok, symbols: {SYM: {bar_rows, ...}}}."""
    resp = _get_json("/readiness/bar-aggregate", {"window_days": str(window_days)}, timeout=60)
    return resp.get("symbols", {})


def get_readiness_latest_bar_via_plugin(
    lookback_days: int = 90,
    symbols: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """GET /readiness/latest-bar-per-symbol → {ok, symbols: {SYM: {bar_date, close}}}."""
    params: Dict[str, str] = {"lookback_days": str(lookback_days)}
    if symbols:
        params["symbols"] = ",".join(symbols)
    resp = _get_json("/readiness/latest-bar-per-symbol", params, timeout=60)
    return resp.get("symbols", {})


def get_readiness_latest_bar_full_via_plugin(
    symbols: List[str],
) -> Dict[str, Dict[str, Any]]:
    """GET /readiness/latest-bar-full-history → {ok, symbols: {SYM: {bar_date, close}}}."""
    if not symbols:
        return {}
    resp = _get_json(
        "/readiness/latest-bar-full-history",
        {"symbols": ",".join(symbols)},
        timeout=60,
    )
    return resp.get("symbols", {})


def get_readiness_financials_coverage_via_plugin() -> Dict[str, Any]:
    """GET /readiness/financials-coverage-symbols → coverage sets."""
    return _get_json("/readiness/financials-coverage-symbols", timeout=45)


def get_readiness_financials_fill_rate_via_plugin(
    universe_symbols: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """GET /readiness/financials-fill-rate → fill rate counts."""
    params: Dict[str, str] = {}
    if universe_symbols:
        params["universe_symbols"] = ",".join(universe_symbols)
    return _get_json("/readiness/financials-fill-rate", params, timeout=60)


def get_readiness_date_coverage_via_plugin(
    days_back: int = 420,
    min_symbols: int = 1000,
) -> Dict[str, Any]:
    """GET /readiness/date-coverage → low coverage dates."""
    return _get_json(
        "/readiness/date-coverage",
        {"days_back": str(days_back), "min_symbols": str(min_symbols)},
        timeout=60,
    )
