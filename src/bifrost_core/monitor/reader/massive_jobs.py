"""Massive-backed option bars in PostgreSQL (market.option_* schema)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.connection import _get_conn_params

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
    """OHLC for one option contract from market.option_daily (1 D) or market.option_minute.

    ``source`` is accepted for API compatibility but ignored (single Polygon vendor).
    Response includes ``source='massive'`` for downstream callers.
    """
    _ = source  # unused — market.* has no source column
    if not config or (config.get("sink") != "postgres" and not config.get("postgres")):
        return []
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
    try:
        params = _get_conn_params(config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if per.upper() == "1 D":
                    cur.execute(
                        """
                        SELECT extract(epoch from bar_date) AS time,
                               open, high, low, close, volume, vwap,
                               'massive' AS source
                        FROM market.option_daily
                        WHERE underlying = %s AND expiry = %s
                          AND strike = %s AND option_right = %s
                        ORDER BY bar_date DESC NULLS LAST
                        LIMIT %s
                        """,
                        (sym, exp, float(strike), r, max(1, min(500, limit))),
                    )
                else:
                    cur.execute(
                        """
                        SELECT extract(epoch from bar_time) AS time,
                               open, high, low, close, volume, vwap,
                               'massive' AS source
                        FROM market.option_minute
                        WHERE underlying = %s AND expiry = %s
                          AND strike = %s AND option_right = %s
                          AND period = %s
                        ORDER BY bar_time DESC NULLS LAST
                        LIMIT %s
                        """,
                        (
                            sym,
                            exp,
                            float(strike),
                            r,
                            _minute_period_db(per),
                            max(1, min(500, limit)),
                        ),
                    )
                rows = cur.fetchall()
            return [dict(x) for x in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.debug("get_option_bars failed: %s", e)
        return []
