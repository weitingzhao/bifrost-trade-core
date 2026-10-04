"""Market: OHLC bars, backfill jobs, trading day and holidays. Conn-based and status_config-based APIs."""

import logging
import math
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.brokerage_tables import CONTRACT_QUOTE_LIVE
from bifrost_core.portfolio.quote_freshness import fresh_quote_sql
from bifrost_core.monitor.reader import write_support as ws

logger = logging.getLogger(__name__)


# UI / API period labels → market.stock_minute.period values written by Polygon ingest.
_MINUTE_PERIOD_TO_DB: Dict[str, str] = {
    "1 min": "1 minute",
    "1 minute": "1 minute",
    "5 mins": "5 minute",
    "5 min": "5 minute",
    "5 minutes": "5 minute",
    "5 minute": "5 minute",
    "1 hour": "1 hour",
    "1 hours": "1 hour",
}


def _minute_period_db(period: str) -> str:
    """Map API period label to market.stock_minute.period."""
    per = (period or "").strip()
    return _MINUTE_PERIOD_TO_DB.get(per, per)


# ----- Conn-based (for common.StatusReader delegation) -----

def get_market_holidays_conn(
    conn: Any, exchange: Optional[str] = None, year: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Return holidays from market.us_market_holiday (FDW). Optional exchange and year filters.

    If exchange is None or empty, returns all exchanges. ``source`` is always ``polygon``
    for API compatibility with the retired public.reference_us_holidays shape.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            base = """SELECT exchange, holiday_date::text AS holiday_date,
                              name AS label,
                              name, status,
                              open_time, close_time,
                              'polygon'::text AS source
                       FROM market.us_market_holiday"""
            where_parts = []
            params: list = []
            if exchange:
                where_parts.append("exchange = %s")
                params.append(exchange)
            if year is not None:
                where_parts.append("EXTRACT(YEAR FROM holiday_date) = %s")
                params.append(year)
            if where_parts:
                base += " WHERE " + " AND ".join(where_parts)
            base += " ORDER BY holiday_date, exchange"
            cur.execute(base, params)
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("get_market_holidays_conn failed: %s", e)
        return []


def get_bars(
    conn: Any,
    symbol: Optional[str] = None,
    period: str = "1 D",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return rows from market.stock_daily (1 D) or market.stock_minute via Plugin API. Newest first."""
    if not symbol or not symbol.strip():
        return []
    try:
        from bifrost_core.monitor.market_read_client import get_bars_via_plugin

        rows = get_bars_via_plugin(symbol.strip(), period=period, limit=limit)
        return rows
    except Exception as e:
        logger.debug("get_bars via plugin failed: %s", e)
        return []


def get_bars_benchmark(
    conn: Any,
    symbols: Optional[List[str]] = None,
    on_or_before: Optional[date] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return latest daily bar on or before given date per symbol via Plugin API."""
    sym_list = list({(s or "").strip() for s in (symbols or []) if (s or "").strip()})
    if not sym_list:
        return {}
    ref_str = (on_or_before if on_or_before is not None else date.today()).isoformat()
    try:
        from bifrost_core.monitor.market_read_client import get_bars_benchmark_via_plugin

        return get_bars_benchmark_via_plugin(sym_list, on_or_before=ref_str)
    except Exception as e:
        logger.debug("get_bars_benchmark via plugin failed: %s", e)
        return {}


def get_stock_day_fallback_price(conn: Any, symbol: str) -> Optional[Tuple[float, float, Optional[float]]]:
    """Return (close, bar_time_epoch, prev_close) from Plugin API when live quote is missing/stale."""
    if not (symbol or "").strip():
        return None
    sym = (symbol or "").strip().upper()
    try:
        from bifrost_core.monitor.market_read_client import get_fallback_price_via_plugin

        resp = get_fallback_price_via_plugin(sym)
        if not resp.get("found"):
            return None
        close = resp.get("close")
        bar_time = resp.get("bar_time")
        prev_close = resp.get("prev_close")
        if close is None or bar_time is None:
            return None
        c = float(close)
        t = float(bar_time)
        if not math.isfinite(c) or not math.isfinite(t) or c <= 0:
            return None
        pcl: Optional[float] = None
        if prev_close is not None:
            try:
                pc = float(prev_close)
                if math.isfinite(pc) and pc > 0:
                    pcl = pc
            except (TypeError, ValueError):
                pass
        return (c, t, pcl)
    except Exception as e:
        logger.debug("get_stock_day_fallback_price via plugin failed: %s", e)
        return None


def get_contract_quotes_conn(conn: Any, contract_keys: List[str]) -> List[Dict[str, Any]]:
    """Return bid/ask/last/mid from contract_quote_live for given contract_keys. Used by GET /quotes for OPT rows."""
    if not contract_keys:
        return []
    keys = [k for k in contract_keys if k and str(k).strip()]
    if not keys:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            placeholders = ", ".join("%s" for _ in keys)
            cur.execute(
                f"""
                SELECT contract_key, symbol, sec_type, expiry, strike, option_right, bid, ask, last, mid,
                       extract(epoch from updated_at) AS ts
                FROM {CONTRACT_QUOTE_LIVE} q
                WHERE contract_key IN (""" + placeholders + """)
                  AND """ + fresh_quote_sql("q") + """
                """,
                tuple(keys),
            )
            rows = cur.fetchall()
        return [
            {
                "contract_key": r["contract_key"],
                "symbol": r["symbol"],
                "sec_type": r["sec_type"],
                "expiry": r["expiry"],
                "strike": r["strike"],
                "option_right": r["option_right"],
                "bid": float(r["bid"]) if r["bid"] is not None else None,
                "ask": float(r["ask"]) if r["ask"] is not None else None,
                "last": float(r["last"]) if r["last"] is not None else None,
                "mid": float(r["mid"]) if r["mid"] is not None else None,
                "ts": float(r["ts"]) if r["ts"] is not None else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.debug("get_contract_quotes_conn failed: %s", e)
        return []


def get_bars_stats(conn: Any, symbol: Optional[str] = None) -> Dict[str, Any]:
    """Return row counts for the given symbol via Plugin API.

    Response keys keep legacy names (``stock_day`` / ``stock_min``) for API compatibility.
    """
    if not symbol or not symbol.strip():
        return {"stock_day": 0, "stock_min": {}}
    try:
        from bifrost_core.monitor.market_read_client import get_bars_stats_via_plugin

        resp = get_bars_stats_via_plugin(symbol.strip())
        return {
            "stock_day": resp.get("stock_day", 0),
            "stock_min": resp.get("stock_min", {}),
        }
    except Exception as e:
        logger.debug("get_bars_stats via plugin failed: %s", e)
        return {"stock_day": 0, "stock_min": {}}


def _coverage_day_iso(v: Any) -> Optional[str]:
    """Normalize MIN/MAX(bar_date) for JSON: always YYYY-MM-DD string."""
    if v is None:
        return None
    if hasattr(v, "isoformat") and callable(getattr(v, "isoformat")):
        try:
            s = v.isoformat()
            return s[:10] if len(s) >= 10 else str(v).strip() or None
        except Exception:
            pass
    s = str(v).strip()
    return s[:10] if len(s) >= 10 else (s or None)


def distinct_caret_symbols_in_stock_bars_tables(conn: Any) -> List[str]:
    """Symbols starting with ``^`` via Plugin API."""
    try:
        from bifrost_core.monitor.market_read_client import get_caret_symbols_via_plugin

        return get_caret_symbols_via_plugin()
    except Exception as e:
        logger.debug("distinct_caret_symbols via plugin failed: %s", e)
        return []


# ----- Module-level (status_config) for re-export -----

def get_market_holidays(status_config: dict, exchange: Optional[str] = None, year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return list of holidays from market.us_market_holiday. exchange=None returns all exchanges."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return []
    try:
        conn = ws.open_conn(status_config)
        try:
            return get_market_holidays_conn(conn, exchange=exchange, year=year)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("get_market_holidays failed: %s", e)
        return []


__all__ = [
    "get_market_holidays",
]
