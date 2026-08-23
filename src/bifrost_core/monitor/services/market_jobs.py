"""Stock OHLC enqueue helpers via Market Data Plugin (ops_jobs)."""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TOLERANCE_END_SEC_TRADING_DAY = 1 * 86400
TOLERANCE_END_SEC_NON_TRADING = 2 * 86400
WATCHLIST_EOD_PERIODS = ["1 D", "1 hour", "5 mins", "1 min"]

_VALID_PERIODS = frozenset(WATCHLIST_EOD_PERIODS)

# API period label → Plugin ingest kind + minute bar params
_PERIOD_PLUGIN: dict[str, tuple[str, Optional[int], Optional[str]]] = {
    "1 D": ("stock_daily", None, None),
    "1 min": ("stock_minute", 1, "minute"),
    "5 mins": ("stock_minute", 5, "minute"),
    "1 hour": ("stock_minute", 1, "hour"),
}


def coverage_status(
    min_ts: Optional[float],
    max_ts: Optional[float],
    count: int,
    target_start_ts: float,
    target_end_ts: float,
    tolerance_end_sec: float,
) -> str:
    """Return ok | gap_end | missing. Only end gap is checked."""
    if count == 0:
        return "missing"
    gap_end = max_ts is None or max_ts < target_end_ts - tolerance_end_sec
    if gap_end:
        return "gap_end"
    return "ok"


def get_watchlist_stock_symbols(reader: Any) -> List[str]:
    """Return unique stock symbols from Watchlist in insertion order."""
    watchlist = reader.get_watchlist()
    sym_list: List[str] = []
    for w in watchlist:
        sec = (w.get("sec_type") or "STK").strip().upper()
        if sec == "OPT":
            continue
        sym = (w.get("symbol") or "").strip()
        if not sym and w.get("contract_key"):
            parts = (w["contract_key"] or "").split("|")
            sym = (parts[0] or "").strip() if parts else ""
        if sym:
            sym_list.append(sym.upper())
    return list(dict.fromkeys(sym_list))


def _read_history_backfill_config() -> dict[str, Any]:
    try:
        from bifrost_core.config.startup import read_config

        config, _ = read_config()
        return (config.get("history_backfill") or {}).get("stock") or {}
    except Exception:
        return {}


def _resolve_span_days(period_key: str, years: Optional[float], days: Optional[int], span_hours: Optional[float]) -> float:
    if span_hours is not None and span_hours > 0:
        return span_hours / 24.0
    if days is not None and days > 0:
        return float(days)
    if years is not None and years > 0:
        return 365.0 * years
    hb = _read_history_backfill_config()
    if period_key == "1D":
        return 365.0 * float(hb.get("daily_years", 10.0))
    if period_key == "1min":
        return 7.0 * float(hb.get("min_weeks", 1.0))
    if period_key == "5min":
        return 30.0 * float(hb.get("5min_months", 1.0))
    return 30.0 * float(hb.get("1hour_months", 3.0))


def _period_key(period: str) -> str:
    per = (period or "1 D").strip()
    period_map = {"1 D": "1D", "1 min": "1min", "5 mins": "5min", "1 hour": "1h"}
    return period_map.get(per) or "1D"


def _date_range_for_backfill(
    reader: Any,
    symbol: str,
    period: str,
    *,
    years: Optional[float] = None,
    days: Optional[int] = None,
    override_days: Optional[float] = None,
    span_hours: Optional[float] = None,
) -> Tuple[date, date, str]:
    """Return (from_date, to_date, mode)."""
    sym = (symbol or "").strip().upper()
    per = (period or "1 D").strip()
    period_key = _period_key(per)
    today = date.today()
    latest_ts = reader.get_bars_latest(symbol=sym, period=per)
    if latest_ts is not None:
        override_sec = (override_days or 0.0) * 86400.0
        start_ts = float(latest_ts) - override_sec
        end_ts = time.time()
        if start_ts >= end_ts:
            return today, today, "noop"
        from_d = datetime.fromtimestamp(start_ts, tz=timezone.utc).date()
        return from_d, today, "incremental_override"
    span_days = _resolve_span_days(period_key, years, days, span_hours)
    from_d = today - timedelta(days=int(span_days))
    return from_d, today, "initial_backfill"


def _plugin_payload(symbol: str, period: str, from_d: date, to_d: date) -> Tuple[str, Dict[str, Any]]:
    per = (period or "1 D").strip()
    if per not in _VALID_PERIODS:
        raise ValueError(f"invalid period: {per}")
    kind, mult, timespan = _PERIOD_PLUGIN[per]
    from_s = from_d.isoformat()
    to_s = to_d.isoformat()
    if kind == "stock_daily":
        return kind, {"symbol": symbol, "from": from_s, "to": to_s}
    return kind, {
        "symbol": symbol,
        "from": from_s,
        "to": to_s,
        "multiplier": mult,
        "timespan": timespan,
    }


def enqueue_plugin_bars_backfill(
    reader: Any,
    symbol: str,
    period: str,
    *,
    years: Optional[float] = None,
    days: Optional[int] = None,
    override_days: Optional[float] = None,
    span_hours: Optional[float] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Enqueue one Plugin ingest job for symbol+period. Returns (ok, job_id, error)."""
    from bifrost_core.monitor.market_write_client import post_ingest_enqueue

    sym = (symbol or "").strip().upper()
    per = (period or "1 D").strip()
    if not sym:
        return False, None, "Missing symbol."
    if per not in _VALID_PERIODS:
        return False, None, f"invalid period: {per}"

    try:
        from_d, to_d, mode = _date_range_for_backfill(
            reader, sym, per, years=years, days=days, override_days=override_days, span_hours=span_hours
        )
    except Exception as e:
        return False, None, str(e)

    if mode == "noop":
        return True, None, "Already have data and no new bars in range; nothing to backfill."

    try:
        kind, payload = _plugin_payload(sym, per, from_d, to_d)
        resp = post_ingest_enqueue(kind, payload)
    except Exception as e:
        logger.warning("enqueue_plugin_bars_backfill failed: %s", e)
        return False, None, str(e)

    if not resp.get("ok"):
        err = resp.get("error") or resp.get("detail") or "plugin enqueue failed"
        return False, None, str(err)

    job_id = resp.get("job_id")
    logger.info(
        "bars/backfill plugin enqueue job_id=%s symbol=%s period=%s kind=%s from=%s to=%s",
        job_id,
        sym,
        per,
        kind,
        from_d,
        to_d,
    )
    return True, str(job_id) if job_id is not None else None, None


def enqueue_watchlist_eod_plugin(
    reader: Any,
    *,
    override_days: float = 1.0,
    periods: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Enqueue Plugin jobs for watchlist symbols × periods (Polygon path)."""
    symbols = get_watchlist_stock_symbols(reader)
    per_list = list(periods or WATCHLIST_EOD_PERIODS)
    if not symbols:
        return {
            "ok": True,
            "queued_count": 0,
            "failed_count": 0,
            "symbols_count": 0,
            "symbols": [],
            "periods": per_list,
            "override_days": override_days,
            "queued_jobs": [],
            "failures": [],
            "message": "No stock symbols in Watchlist; nothing to enqueue.",
        }

    queued_jobs: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for sym in symbols:
        for per in per_list:
            ok, job_id, error = enqueue_plugin_bars_backfill(
                reader, sym, per, override_days=override_days
            )
            if ok and job_id:
                queued_jobs.append({"job_id": job_id, "symbol": sym, "period": per})
            elif ok and not job_id:
                continue  # noop
            else:
                failures.append({"symbol": sym, "period": per, "error": error or "Enqueue failed."})

    queued_count = len(queued_jobs)
    failed_count = len(failures)
    ok = queued_count > 0 or (failed_count == 0 and len(symbols) > 0)
    message = f"Queued {queued_count} Plugin ingest job(s) for {len(symbols)} watchlist symbol(s)."
    if failed_count:
        message += f" Failed: {failed_count}."
    return {
        "ok": ok,
        "message": message,
        "queued_count": queued_count,
        "failed_count": failed_count,
        "symbols_count": len(symbols),
        "symbols": symbols,
        "periods": per_list,
        "override_days": override_days,
        "queued_jobs": queued_jobs,
        "failures": failures,
    }


def build_plugin_backfill_preview(
    reader: Any,
    symbol: str,
    period: str,
    override_days: Optional[float] = None,
) -> Dict[str, Any]:
    """Preview Plugin enqueue plan (no IB requests)."""
    sym = (symbol or "").strip().upper()
    per = (period or "1 D").strip()
    try:
        from_d, to_d, mode = _date_range_for_backfill(reader, sym, per, override_days=override_days)
        latest_ts = reader.get_bars_latest(symbol=sym, period=per)
        kind, payload = _plugin_payload(sym, per, from_d, to_d)
        return {
            "symbol": sym,
            "period": per,
            "mode": mode,
            "latest_ts": float(latest_ts) if latest_ts is not None else None,
            "plugin_enqueue": {"kind": kind, "payload": payload},
            "fetch_start_date": from_d.isoformat(),
            "fetch_end_date": to_d.isoformat(),
        }
    except Exception as e:
        logger.warning("build_plugin_backfill_preview failed: %s", e)
        return {"symbol": sym, "period": per, "ok": False, "error": str(e)}
