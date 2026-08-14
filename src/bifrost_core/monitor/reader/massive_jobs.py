"""Massive-backed option bars via Plugin Market Data API."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MINUTE_PERIOD_TO_DB = {
    "1 min": "1 minute",
    "1 minute": "1 minute",
    "5 mins": "5 minute",
    "5 min": "5 minute",
    "5 minutes": "5 minute",
    "5 minute": "5 minute",
    "1 hour": "1 hour",
}


def _norm_expiry_date(expiry: str) -> Optional[date]:
    """Normalize expiry to date. Accepts YYYY-MM-DD or YYYYMMDD."""
    e = (expiry or "").strip()
    if not e:
        return None
    if len(e) >= 10 and e[4] == "-":
        try:
            return date.fromisoformat(e[:10])
        except ValueError:
            return None
    digits = "".join(c for c in e if c.isdigit())
    if len(digits) >= 8:
        try:
            return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def _minute_period_db(period: str) -> str:
    per = (period or "").strip()
    return _MINUTE_PERIOD_TO_DB.get(per, per)


def get_option_bars(
    config: dict,
    symbol: str,
    expiry: str,
    strike: float,
    option_right: str,
    *,
    period: str = "1 min",
    source: str = "massive",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """OHLC for one option contract via Plugin API (option_daily / option_minute).

    ``config`` and ``source`` are accepted for API compatibility.
    Response includes ``source='massive'`` for downstream callers.
    """
    from bifrost_core.monitor.market_read_client import (
        get_option_bars_daily_via_plugin,
        get_option_bars_minute_via_plugin,
    )

    per = (period or "1 min").strip()
    sym = (symbol or "").strip().upper()
    exp = _norm_expiry_date(expiry)
    r = (option_right or "").strip().upper()
    if r in ("CALL",):
        r = "C"
    if r in ("PUT",):
        r = "P"
    if not sym or exp is None:
        return []
    exp_str = exp.isoformat()
    try:
        if per.upper() == "1 D":
            raw = get_option_bars_daily_via_plugin(sym, exp_str, float(strike), r, limit=limit)
            rows: List[Dict[str, Any]] = []
            for bar in raw:
                bd = bar.get("bar_date")
                epoch = None
                if bd:
                    try:
                        epoch = date.fromisoformat(str(bd)[:10]).toordinal()
                        d = date.fromisoformat(str(bd)[:10])
                        from datetime import datetime, timezone as tz
                        epoch = datetime(d.year, d.month, d.day, tzinfo=tz.utc).timestamp()
                    except (ValueError, TypeError):
                        pass
                rows.append({
                    "time": epoch,
                    "open": bar.get("open"),
                    "high": bar.get("high"),
                    "low": bar.get("low"),
                    "close": bar.get("close"),
                    "volume": bar.get("volume"),
                    "vwap": bar.get("vwap"),
                    "source": "massive",
                })
            return rows
        else:
            db_period = _minute_period_db(per)
            raw = get_option_bars_minute_via_plugin(
                sym, exp_str, float(strike), r, period=db_period, limit=limit,
            )
            return [
                {**bar, "source": "massive"}
                for bar in raw
            ]
    except Exception as e:
        logger.debug("get_option_bars via Plugin failed: %s", e)
        return []
