"""Bars backfill job helpers and coverage utilities (monitor domain, no FastAPI)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from bifrost_core.monitor.reader import (
    insert_job_bars_backfill,
    update_job_bars_backfill_result,
)

logger = logging.getLogger(__name__)

TOLERANCE_END_SEC_TRADING_DAY = 1 * 86400
TOLERANCE_END_SEC_NON_TRADING = 2 * 86400
WATCHLIST_EOD_PERIODS = ["1 D", "1 hour", "5 mins", "1 min"]


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


def job_row_to_api(j: Dict[str, Any]) -> Dict[str, Any]:
    """Map DB row to API shape (job_id, created_ts, updated_ts, ...)."""
    created_ts = j.get("created_at")
    if hasattr(created_ts, "timestamp"):
        created_ts = created_ts.timestamp()
    updated_ts = j.get("updated_at")
    if hasattr(updated_ts, "timestamp"):
        updated_ts = updated_ts.timestamp()
    return {
        "job_id": str(j.get("job_bars_backfill_id", "")),
        "type": "backfill",
        "symbol": j.get("symbol"),
        "period": j.get("period"),
        "years": j.get("years"),
        "days": j.get("days"),
        "override_days": j.get("override_days"),
        "status": j.get("status"),
        "result": j.get("result"),
        "created_ts": created_ts,
        "updated_ts": updated_ts,
    }


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


def enqueue_job_bars_backfill(
    control_via_db: Any,
    symbol: str,
    period: str,
    *,
    years: Optional[float] = None,
    days: Optional[int] = None,
    override_days: Optional[float] = None,
    span_hours: Optional[float] = None,
    is_test: bool = False,
    api_interval_sec: int = 10,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """Insert one job_bars_backfill row and enqueue the matching Celery task."""
    jid = insert_job_bars_backfill(
        control_via_db,
        symbol,
        period,
        years,
        days,
        override_days,
        span_hours=span_hours,
        skip_ib=is_test,
        api_interval_sec=api_interval_sec,
    )
    if jid is None:
        return False, None, "Enqueue failed."
    logger.info(
        "bars/backfill enqueue job_id=%s symbol=%s period=%s years=%s days=%s override_days=%s span_hours=%s",
        jid,
        symbol,
        period,
        years,
        days,
        override_days,
        span_hours,
    )
    try:
        from src.bars.tasks import backfill_bars

        backfill_bars.apply_async(
            args=[symbol, period],
            kwargs={
                "years": years,
                "days": days,
                "override_days": override_days,
                "span_hours": span_hours,
            },
            task_id=str(jid),
        )
    except Exception as e:
        logger.exception("Celery enqueue failed: %s", e)
        update_job_bars_backfill_result(control_via_db, jid, "failed", {"ok": False, "error": str(e)})
        return False, None, f"Celery enqueue failed: {e}"
    return True, str(jid), None


def reenqueue_bars_backfill_from_row(control_via_db: Any, row: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Submit Celery ``backfill_bars`` for an existing row (same ``task_id`` as ``job_bars_backfill_id``)."""
    try:
        jid = int(row["job_bars_backfill_id"])
    except (TypeError, ValueError, KeyError):
        return False, "invalid_job_id"
    symbol = (row.get("symbol") or "").strip()
    period = (row.get("period") or "1 D").strip()
    if not symbol:
        return False, "missing_symbol"

    def _opt_float(key: str) -> Optional[float]:
        v = row.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _opt_int(key: str) -> Optional[int]:
        v = row.get(key)
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    years = _opt_float("years")
    days = _opt_int("days")
    override_days = _opt_float("override_days")
    span_hours = _opt_float("span_hours")
    logger.info(
        "bars/backfill re-enqueue job_id=%s symbol=%s period=%s years=%s days=%s override_days=%s span_hours=%s",
        jid,
        symbol,
        period,
        years,
        days,
        override_days,
        span_hours,
    )
    try:
        from src.bars.tasks import backfill_bars

        backfill_bars.apply_async(
            args=[symbol, period],
            kwargs={
                "years": years,
                "days": days,
                "override_days": override_days,
                "span_hours": span_hours,
            },
            task_id=str(jid),
        )
    except Exception as e:
        # Don't revert the job to failed — leave it as pending so the user can
        # retry again later or a Worker can pick it up once started.
        logger.exception("Celery re-enqueue failed job_id=%s (job stays pending): %s", jid, e)
        return False, str(e)
    return True, None
