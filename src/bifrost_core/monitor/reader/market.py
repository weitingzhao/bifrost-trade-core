"""Market: OHLC bars, backfill jobs, trading day and holidays. Conn-based and status_config-based APIs."""

import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.brokerage_tables import CONTRACT_QUOTE_LIVE
from bifrost_core.persistence.postgres.connection import _get_conn_params

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

def get_is_us_trading_day_conn(conn: Any, date_str: str) -> bool:
    """Return True if the given date (YYYY-MM-DD) is a US (NYSE) trading day."""
    try:
        d = date.fromisoformat(date_str)
        if d.weekday() >= 5:
            return False
    except (ValueError, TypeError):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM market.us_market_holiday
                   WHERE exchange = 'NYSE' AND holiday_date = %s
                     AND status = 'closed'
                   LIMIT 1""",
                (d,),
            )
            row = cur.fetchone()
        return row is None
    except Exception as e:
        logger.debug("get_is_us_trading_day_conn failed: %s", e)
        return True


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


def get_bars_latest(conn: Any, symbol: Optional[str] = None, period: str = "1 D") -> Optional[float]:
    """Return Unix time of the latest bar for symbol+period via Plugin API, or None if no data."""
    if not symbol or not symbol.strip():
        return None
    try:
        from bifrost_core.monitor.market_read_client import get_bars_latest_via_plugin

        return get_bars_latest_via_plugin(symbol.strip(), period=period)
    except Exception as e:
        logger.debug("get_bars_latest via plugin failed: %s", e)
        return None


def get_bar_times_in_range(
    conn: Any,
    symbol: Optional[str] = None,
    period: str = "1 D",
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> List[float]:
    """Return bar timestamps within [start_ts, end_ts] ordered ascending via Plugin API."""
    if not symbol or not symbol.strip() or start_ts is None or end_ts is None:
        return []
    try:
        from bifrost_core.monitor.market_read_client import get_bar_times_in_range_via_plugin

        return get_bar_times_in_range_via_plugin(
            symbol.strip(), period=period, start_ts=float(start_ts), end_ts=float(end_ts)
        )
    except Exception as e:
        logger.debug("get_bar_times_in_range via plugin failed: %s", e)
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
                FROM {CONTRACT_QUOTE_LIVE}
                WHERE contract_key IN (""" + placeholders + """)
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


def _ordered_unique_symbols(symbols: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for s in symbols or []:
        t = (s or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def distinct_caret_symbols_in_stock_bars_tables(conn: Any) -> List[str]:
    """Symbols starting with ``^`` via Plugin API."""
    try:
        from bifrost_core.monitor.market_read_client import get_caret_symbols_via_plugin

        return get_caret_symbols_via_plugin()
    except Exception as e:
        logger.debug("distinct_caret_symbols via plugin failed: %s", e)
        return []


def get_bars_coverage(conn: Any, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return per-symbol coverage via Plugin API.

    Response keys keep legacy names (``stock_day`` / ``stock_min``) for API compatibility.
    """
    sym_list = _ordered_unique_symbols(list(symbols) if symbols else None)
    if not sym_list:
        return []
    try:
        from bifrost_core.monitor.market_read_client import get_bars_coverage_via_plugin

        return get_bars_coverage_via_plugin(sym_list)
    except Exception as e:
        logger.debug("get_bars_coverage via plugin failed: %s", e)
        return []


# ----- Module-level (status_config) for re-export -----

def write_ohlc_bars_to_db(status_config: dict, rows: List[Dict[str, Any]]) -> bool:
    """Write OHLC bars via Plugin Market Data API (POST /stocks/bars/ingest).

    Each row is normalised to ``{symbol, period, bar_time, open, high, low, close, volume}``
    with ``bar_time`` as ISO-8601 string. Daily bars preserve the original ``bar_date`` to
    avoid UTC date shift.

    Plugin API accepts raw period labels (``1 D``, ``1 min``, ``5 mins``, ``1 hour``).
    """
    if not rows:
        return False
    try:
        from bifrost_core.monitor.market_write_client import post_bars_ingest

        payload: List[Dict[str, Any]] = []
        for r in rows:
            symbol = (r.get("symbol") or "").strip()
            period = (r.get("period") or "1 D").strip()
            bar_time = r.get("bar_time")
            if bar_time is None or not symbol:
                continue

            if isinstance(bar_time, (int, float)):
                bar_dt = datetime.fromtimestamp(float(bar_time), tz=timezone.utc)
            else:
                bar_dt = bar_time

            if period.upper() == "1 D":
                bar_date_str = r.get("bar_date")
                if bar_date_str:
                    bt_iso = str(bar_date_str)[:10]
                elif isinstance(bar_dt, datetime):
                    bt_iso = bar_dt.strftime("%Y-%m-%d")
                else:
                    bt_iso = str(bar_dt)
            else:
                bt_iso = bar_dt.isoformat() if isinstance(bar_dt, datetime) else str(bar_dt)

            payload.append({
                "symbol": symbol,
                "period": period,
                "bar_time": bt_iso,
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r.get("close"),
                "volume": r.get("volume"),
            })

        if not payload:
            return False

        resp = post_bars_ingest(payload)
        written = resp.get("written", len(payload))
        logger.info(
            "[R-A3] write_ohlc_bars_to_db: wrote %s rows via Plugin API",
            written,
        )
        return True
    except Exception as e:
        logger.warning("write_ohlc_bars_to_db failed: %s", e)
        return False


def write_stock_bars(status_config: dict, symbol: str, period: str, bars: List[Dict[str, Any]]) -> bool:
    """Batch write bars for one symbol+period. Thin wrapper over write_ohlc_bars_to_db."""
    if not bars:
        return True
    per = (period or "1 D").strip()
    sym = (symbol or "").strip()
    if not sym:
        return False
    rows = []
    for b in bars:
        r = dict(b)
        r["symbol"] = sym
        r["period"] = per
        rows.append(r)
    return write_ohlc_bars_to_db(status_config, rows)


def delete_stock_bars_for_symbol(
    status_config: dict,
    symbol: str,
    periods: Optional[list] = None,
) -> Dict[str, Any]:
    """Delete bars for a symbol via Plugin Market Data API (DELETE /stocks/bars).

    Returns ``{ok, deleted_day, deleted_min}`` or ``{ok: False, error}``.
    """
    sym = (symbol or "").strip()
    if not sym:
        return {"ok": False, "error": "Symbol required"}
    valid_periods = {"1 D", "1 min", "5 mins", "1 hour"}
    if periods:
        periods = [p.strip() for p in periods if (p or "").strip() in valid_periods]
    delete_daily = not periods or "1 D" in periods
    min_periods = [p for p in ("1 min", "5 mins", "1 hour") if not periods or p in periods]
    try:
        from bifrost_core.monitor.market_write_client import delete_bars

        resp = delete_bars(
            symbol=sym,
            delete_daily=delete_daily,
            periods=min_periods if min_periods else None,
        )
        deleted_day = resp.get("deleted_daily", 0)
        deleted_min = resp.get("deleted_minute", 0)
        logger.info(
            "delete_stock_bars_for_symbol %s periods=%s: deleted_day=%s deleted_min=%s",
            sym, periods, deleted_day, deleted_min,
        )
        return {"ok": True, "deleted_day": deleted_day, "deleted_min": deleted_min}
    except Exception as e:
        logger.warning("delete_stock_bars_for_symbol failed: %s", e)
        return {"ok": False, "error": str(e)}



def get_is_us_trading_day(status_config: dict, date_str: str) -> bool:
    """Return True if the given date (YYYY-MM-DD) is a US (NYSE) trading day."""
    try:
        d = date.fromisoformat(date_str)
        if d.weekday() >= 5:
            return False
    except (ValueError, TypeError):
        return False
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return True
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            return get_is_us_trading_day_conn(conn, date_str)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("get_is_us_trading_day failed: %s", e)
        return True


def get_market_holidays(status_config: dict, exchange: Optional[str] = None, year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return list of holidays from market.us_market_holiday. exchange=None returns all exchanges."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return []
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            return get_market_holidays_conn(conn, exchange=exchange, year=year)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("get_market_holidays failed: %s", e)
        return []


__all__ = [
    "write_ohlc_bars_to_db",
    "write_stock_bars",
    "delete_stock_bars_for_symbol",
    "get_is_us_trading_day",
    "get_market_holidays",
]
