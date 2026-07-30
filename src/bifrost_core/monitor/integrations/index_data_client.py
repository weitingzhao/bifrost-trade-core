"""Fetch US reference index daily bars via Massive/Polygon v2 aggs and write to market.stock_daily.

Used by POST /indices/refresh. Gap-fill from DB latest daily bar; UPSERT via write_ohlc_bars_to_db.
Requires config ``reference_indices`` with ``symbol``, optional ``label``, optional ``polygon_ticker``
(see ``src.massive.polygon_stock_tickers``).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DELAY_BETWEEN_SYMBOLS_SEC = 2.0
DEFAULT_LOOKBACK_DAYS = 30
MAX_LOOKBACK_MS = 10 * 365 * 86400 * 1000


def _range_ms_for_refresh(
    last_bar_ts_sec: Optional[float],
    days: Optional[int],
) -> tuple[int, int]:
    end_ms = int(time.time() * 1000)
    if days is not None and int(days) > 0:
        start_ms = end_ms - int(days) * 86400 * 1000
        return start_ms, end_ms
    if last_bar_ts_sec is not None:
        start_ms = int(last_bar_ts_sec * 1000) - 5 * 86400 * 1000
        return max(start_ms, end_ms - MAX_LOOKBACK_MS), end_ms
    start_ms = end_ms - DEFAULT_LOOKBACK_DAYS * 86400 * 1000
    return start_ms, end_ms


def _aggs_results_to_daily_rows(symbol: str, bars: List[Any]) -> List[Dict[str, Any]]:
    """Convert Massive/Polygon agg results into write_ohlc_bars_to_db row dicts."""
    sym = (symbol or "").strip().upper()
    rows: List[Dict[str, Any]] = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        t = bar.get("t")
        if t is None:
            continue
        try:
            bt = datetime.fromtimestamp(int(t) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        bar_d = bt.date()
        o, h, l_, c, v = bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"), bar.get("v")
        rows.append(
            {
                "symbol": sym,
                "period": "1 D",
                "bar_time": bt,
                "bar_date": bar_d.isoformat(),
                "open": float(o) if o is not None else None,
                "high": float(h) if h is not None else None,
                "low": float(l_) if l_ is not None else None,
                "close": float(c) if c is not None else None,
                "volume": float(v) if v is not None else None,
            }
        )
    return rows


def _run_one_index_massive(
    config: Dict[str, Any],
    item: Dict[str, Any],
    reader: Any,
    *,
    days: Optional[int] = None,
) -> tuple[bool, str, str]:
    """Returns (ok, symbol, error_message)."""
    from src.massive.polygon_stock_tickers import polygon_ticker_for_massive_aggs
    from bifrost_core.monitor.reader.market import write_ohlc_bars_to_db
    from src.vendor.massive.client import MassiveClient
    from src.vendor.massive.config import get_massive_settings

    symbol = (item.get("symbol") or "").strip()
    label = (item.get("label") or symbol) or ""
    if not symbol:
        return False, "", "missing symbol"

    ref_list = config.get("reference_indices") or []
    fetch_t = polygon_ticker_for_massive_aggs(symbol, ref_list)
    if not fetch_t:
        return False, symbol, f"{label}: could not resolve Polygon ticker"

    ms = get_massive_settings(config)
    client = MassiveClient(ms["api_key"], ms["rest_base"])
    if not client.configured:
        return False, symbol, "Massive API key not configured"

    last_ts = reader.get_bars_latest(symbol, "1 D")
    start_ms, end_ms = _range_ms_for_refresh(last_ts, days)

    data = client.fetch_stock_aggs(fetch_t, 1, "day", start_ms, end_ms)
    if data.get("error"):
        return False, symbol, f"{label} ({symbol}): {data.get('error')}"
    bars = data.get("results") or []
    if not isinstance(bars, list) or len(bars) == 0:
        return False, symbol, f"{label} ({symbol}): no daily aggregates returned"

    rows = _aggs_results_to_daily_rows(symbol, bars)
    if not rows:
        return False, symbol, f"{label} ({symbol}): no parseable daily aggregates"

    try:
        ok = write_ohlc_bars_to_db(config, rows)
        if not ok:
            return False, symbol, f"{symbol}: write_ohlc_bars_to_db returned false"
        logger.info(
            "reference_indices: Massive wrote %s daily rows for %s (%s) → market.stock_daily",
            len(rows),
            symbol,
            label,
        )
        return True, symbol, ""
    except Exception as e:
        logger.warning("reference_indices: write failed for %s: %s", symbol, e)
        return False, symbol, f"{symbol}: {e}"


def refresh_reference_indices(
    config: Dict[str, Any],
    *,
    reader: Optional[Any] = None,
    delay_sec: float = DELAY_BETWEEN_SYMBOLS_SEC,
) -> Dict[str, Any]:
    """Fetch all reference indices from Massive/Polygon and write to market.stock_daily.

    config must contain reference_indices (list of { symbol, label?, polygon_ticker? }) and postgres.
    Returns { "ok": bool, "updated": [symbol, ...], "errors": [str, ...] }.
    """
    from bifrost_core.monitor.reader import StatusReader

    indices = config.get("reference_indices") or []
    if not indices:
        return {"ok": True, "updated": [], "errors": []}

    if not config.get("postgres") and not __import__("os").environ.get("PGHOST"):
        return {"ok": False, "updated": [], "errors": ["postgres config required to write index bars"]}

    if reader is None:
        reader = StatusReader(config)

    updated: List[str] = []
    errors: List[str] = []

    for i, item in enumerate(indices):
        if not isinstance(item, dict):
            continue
        if i > 0:
            time.sleep(delay_sec)
        ok, sym, err = _run_one_index_massive(config, item, reader, days=None)
        if ok:
            updated.append(sym)
        elif err:
            errors.append(err)

    return {"ok": len(errors) == 0, "updated": updated, "errors": errors}


def refresh_one_index(
    config: Dict[str, Any],
    symbol: str,
    *,
    days: Optional[int] = None,
    reader: Optional[Any] = None,
) -> Dict[str, Any]:
    """Refresh one reference index by symbol (e.g. ^GSPC). Optional days overrides gap range."""
    from bifrost_core.monitor.reader import StatusReader

    indices = config.get("reference_indices") or []
    sym = (symbol or "").strip()
    if not sym:
        return {"ok": False, "updated": [], "errors": ["symbol required"]}
    item = next((i for i in indices if isinstance(i, dict) and (i.get("symbol") or "").strip() == sym), None)
    if not item:
        return {"ok": False, "updated": [], "errors": [f"symbol {sym} not in reference_indices"]}
    if not config.get("postgres") and not __import__("os").environ.get("PGHOST"):
        return {"ok": False, "updated": [], "errors": ["postgres config required"]}
    if reader is None:
        reader = StatusReader(config)

    ok, out_sym, err = _run_one_index_massive(config, item, reader, days=days)
    if ok:
        return {"ok": True, "updated": [out_sym], "errors": []}
    return {"ok": False, "updated": [], "errors": [err] if err else [sym]}
