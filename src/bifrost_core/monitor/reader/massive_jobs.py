"""Massive-backed option bars in PostgreSQL."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)


def _norm_expiry_db(expiry: str) -> str:
    e = (expiry or "").strip()
    if len(e) >= 10 and e[4] == "-":
        return e[:4] + e[5:7] + e[8:10]
    return e


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
    """OHLC for one option contract from option_day (1 D) or option_min."""
    if not config or (config.get("sink") != "postgres" and not config.get("postgres")):
        return []
    per = (period or "1 min").strip()
    sym = (symbol or "").strip().upper()
    exp = _norm_expiry_db(expiry)
    r = (option_right or "").strip().upper()
    if r in ("CALL",):
        r = "C"
    if r in ("PUT",):
        r = "P"
    if not sym or not exp:
        return []
    src = (source or "massive").strip().lower()
    if src not in ("ib", "massive"):
        src = "massive"
    try:
        params = _get_conn_params(config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if per.upper() == "1 D":
                    cur.execute(
                        """
                        SELECT extract(epoch from bar_time) AS time, open, high, low, close, volume, vwap, source
                        FROM option_day
                        WHERE symbol = %s AND expiry = %s AND strike = %s AND option_right = %s AND source = %s
                        ORDER BY bar_time DESC NULLS LAST
                        LIMIT %s
                        """,
                        (sym, exp, float(strike), r, src, max(1, min(500, limit))),
                    )
                else:
                    cur.execute(
                        """
                        SELECT extract(epoch from bar_time) AS time, open, high, low, close, volume, vwap, source
                        FROM option_min
                        WHERE symbol = %s AND expiry = %s AND strike = %s AND option_right = %s
                          AND period = %s AND source = %s
                        ORDER BY bar_time DESC NULLS LAST
                        LIMIT %s
                        """,
                        (sym, exp, float(strike), r, per, src, max(1, min(500, limit))),
                    )
                rows = cur.fetchall()
            return [dict(x) for x in rows]
        finally:
            conn.close()
    except Exception as e:
        logger.debug("get_option_bars failed: %s", e)
        return []
