"""Refresh US reference index daily bars via Plugin Market Data ingest.

Used by POST /indices/refresh. Enqueues ``stock_daily`` jobs so Plugin workers
write ``market.stock_daily`` in Golden Source. Does not import Trade Massive.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DELAY_BETWEEN_SYMBOLS_SEC = 2.0
DEFAULT_LOOKBACK_DAYS = 30


def _polygon_ticker(item: Dict[str, Any]) -> str:
    raw = (item.get("polygon_ticker") or item.get("symbol") or "").strip().upper()
    return raw


def _lookback_dates(days: Optional[int]) -> tuple[str, str]:
    lookback = int(days) if days is not None and int(days) > 0 else DEFAULT_LOOKBACK_DAYS
    to_d = date.today()
    from_d = to_d - timedelta(days=lookback)
    return from_d.isoformat(), to_d.isoformat()


def _run_one_index_plugin(
    item: Dict[str, Any],
    *,
    days: Optional[int] = None,
) -> tuple[bool, str, str]:
    """Returns (ok, symbol, error_message)."""
    from bifrost_core.monitor.market_write_client import post_ingest_enqueue

    symbol = (item.get("symbol") or "").strip()
    label = (item.get("label") or symbol) or ""
    if not symbol:
        return False, "", "missing symbol"

    ticker = _polygon_ticker(item)
    if not ticker:
        return False, symbol, f"{label}: could not resolve Polygon ticker"
    if ticker.startswith("^"):
        return False, symbol, (
            f"{label} ({symbol}): set polygon_ticker to a Polygon stock ticker "
            "(caret indices are not Plugin stock_daily symbols)"
        )

    from_d, to_d = _lookback_dates(days)
    try:
        resp = post_ingest_enqueue(
            "stock_daily",
            {"symbol": ticker, "from": from_d, "to": to_d},
        )
    except Exception as e:
        logger.warning("reference_indices: plugin enqueue failed for %s: %s", symbol, e)
        return False, symbol, f"{symbol}: {e}"

    if not resp.get("ok"):
        err = resp.get("error") or resp.get("detail") or "plugin enqueue failed"
        return False, symbol, f"{label} ({symbol}): {err}"

    logger.info(
        "reference_indices: Plugin enqueued stock_daily for %s (%s ticker=%s job_id=%s)",
        symbol,
        label,
        ticker,
        resp.get("job_id"),
    )
    return True, symbol, ""


def refresh_reference_indices(
    config: Dict[str, Any],
    *,
    reader: Optional[Any] = None,
    delay_sec: float = DELAY_BETWEEN_SYMBOLS_SEC,
) -> Dict[str, Any]:
    """Enqueue Plugin stock_daily ingest for each reference index.

    config must contain reference_indices (list of { symbol, label?, polygon_ticker? }).
    Returns { "ok": bool, "updated": [symbol, ...], "errors": [str, ...] }.
    ``reader`` is accepted for call-site compatibility and unused.
    """
    del reader
    indices = config.get("reference_indices") or []
    if not indices:
        return {"ok": True, "updated": [], "errors": []}

    updated: list[str] = []
    errors: list[str] = []

    for i, item in enumerate(indices):
        if not isinstance(item, dict):
            continue
        if i > 0:
            time.sleep(delay_sec)
        ok, sym, err = _run_one_index_plugin(item, days=None)
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
    """Refresh one reference index by symbol (e.g. SPY). Optional days overrides lookback."""
    del reader
    indices = config.get("reference_indices") or []
    sym = (symbol or "").strip()
    if not sym:
        return {"ok": False, "updated": [], "errors": ["symbol required"]}
    item = next((i for i in indices if isinstance(i, dict) and (i.get("symbol") or "").strip() == sym), None)
    if not item:
        return {"ok": False, "updated": [], "errors": [f"symbol {sym} not in reference_indices"]}

    ok, out_sym, err = _run_one_index_plugin(item, days=days)
    if ok:
        return {"ok": True, "updated": [out_sym], "errors": []}
    return {"ok": False, "updated": [], "errors": [err] if err else [sym]}
