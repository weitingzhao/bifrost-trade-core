"""Market: OHLC bars, backfill jobs, trading day and holidays. Conn-based and status_config-based APIs."""

import json
import logging
import math
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.connection import _get_conn_params
from bifrost_core.monitor.reader.symbol_normalize import norm_bars_symbol as _norm_bars_symbol

logger = logging.getLogger(__name__)


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
                """SELECT 1 FROM reference_us_holidays
                   WHERE exchange = 'NYSE' AND holiday_date = %s
                     AND (status IS NULL OR status = 'closed')
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
    """Return list of holidays from reference_us_holidays. Optional exchange and year filters.
    If exchange is None or empty, returns all exchanges."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            base = """SELECT exchange, holiday_date::text AS holiday_date,
                              COALESCE(name, label) AS label,
                              name, status,
                              open_time, close_time, source
                       FROM reference_us_holidays"""
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


def add_market_holiday_conn(
    conn: Any, date_str: str, label: Optional[str] = None, exchange: str = "NYSE"
) -> bool:
    """Insert or update one holiday. Returns True on success."""
    try:
        d = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO reference_us_holidays (exchange, holiday_date, label)
                   VALUES (%s, %s, %s) ON CONFLICT (exchange, holiday_date) DO UPDATE SET label = EXCLUDED.label""",
                (exchange, d, (label or "").strip() or None),
            )
        conn.commit()
        return True
    except Exception as e:
        logger.debug("add_market_holiday_conn failed: %s", e)
        return False


def delete_market_holiday_conn(conn: Any, date_str: str, exchange: str = "NYSE") -> bool:
    """Delete one holiday. Returns True on success."""
    try:
        d = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reference_us_holidays WHERE exchange = %s AND holiday_date = %s",
                (exchange, d),
            )
        conn.commit()
        return True
    except Exception as e:
        logger.debug("delete_market_holiday_conn failed: %s", e)
        return False


def get_bars(
    conn: Any,
    symbol: Optional[str] = None,
    period: str = "1 D",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Return rows from stock_day (1 D) or stock_min. Newest first. bar_time as Unix time for API."""
    if not symbol or not symbol.strip():
        return []
    per = (period or "1 D").strip()
    table = "stock_day" if per.upper() == "1 D" else "stock_min"
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if table == "stock_day":
                cur.execute(
                    """
                    SELECT symbol, '1 D' AS period, extract(epoch from bar_time) AS time,
                           open, high, low, close, volume, vwap
                    FROM (
                      SELECT DISTINCT ON (symbol, bar_time)
                        symbol, bar_time, open, high, low, close, volume, vwap
                      FROM stock_day
                      WHERE symbol = %s
                      ORDER BY symbol, bar_time DESC,
                        CASE COALESCE(source, 'ib')
                          WHEN 'ib' THEN 0 WHEN 'tv' THEN 1 WHEN 'massive' THEN 2 ELSE 3 END ASC
                    ) d
                    ORDER BY bar_time DESC NULLS LAST
                    LIMIT %s
                    """,
                    (symbol.strip(), limit),
                )
            else:
                cur.execute(
                    """
                    SELECT symbol, period, extract(epoch from bar_time) AS time,
                           open, high, low, close, volume, vwap
                    FROM (
                      SELECT DISTINCT ON (symbol, period, bar_time)
                        symbol, period, bar_time, open, high, low, close, volume, vwap
                      FROM stock_min
                      WHERE symbol = %s AND period = %s
                      ORDER BY symbol, period, bar_time DESC,
                        CASE COALESCE(source, 'ib')
                          WHEN 'ib' THEN 0 WHEN 'tv' THEN 1 WHEN 'massive' THEN 2 ELSE 3 END ASC
                    ) m
                    ORDER BY bar_time DESC NULLS LAST
                    LIMIT %s
                    """,
                    (symbol.strip(), per, limit),
                )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("get_bars failed: %s", e)
        return []


def get_bars_latest(conn: Any, symbol: Optional[str] = None, period: str = "1 D") -> Optional[float]:
    """Return Unix time of the latest bar for symbol+period, or None if no data."""
    if not symbol or not symbol.strip():
        return None
    per = (period or "1 D").strip()
    table = "stock_day" if per.upper() == "1 D" else "stock_min"
    try:
        with conn.cursor() as cur:
            if table == "stock_day":
                cur.execute(
                    """
                    SELECT extract(epoch from bar_time) AS t
                    FROM (
                      SELECT DISTINCT ON (symbol, bar_time) symbol, bar_time
                      FROM stock_day
                      WHERE symbol = %s
                      ORDER BY symbol, bar_time DESC,
                        CASE COALESCE(source, 'ib')
                          WHEN 'ib' THEN 0 WHEN 'tv' THEN 1 WHEN 'massive' THEN 2 ELSE 3 END ASC
                    ) d
                    ORDER BY bar_time DESC LIMIT 1
                    """,
                    (symbol.strip(),),
                )
            else:
                cur.execute(
                    """
                    SELECT extract(epoch from bar_time) AS t
                    FROM (
                      SELECT DISTINCT ON (symbol, period, bar_time) symbol, period, bar_time
                      FROM stock_min
                      WHERE symbol = %s AND period = %s
                      ORDER BY symbol, period, bar_time DESC,
                        CASE COALESCE(source, 'ib')
                          WHEN 'ib' THEN 0 WHEN 'tv' THEN 1 WHEN 'massive' THEN 2 ELSE 3 END ASC
                    ) m
                    ORDER BY bar_time DESC LIMIT 1
                    """,
                    (symbol.strip(), per),
                )
            row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as e:
        logger.debug("get_bars_latest failed: %s", e)
        return None


def get_bar_times_in_range(
    conn: Any,
    symbol: Optional[str] = None,
    period: str = "1 D",
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> List[float]:
    """Return bar timestamps within [start_ts, end_ts] ordered ascending."""
    if not symbol or not symbol.strip() or start_ts is None or end_ts is None:
        return []
    sym = symbol.strip()
    per = (period or "1 D").strip()
    table = "stock_day" if per.upper() == "1 D" else "stock_min"
    try:
        with conn.cursor() as cur:
            if table == "stock_day":
                cur.execute(
                    """
                    SELECT extract(epoch from bar_time) AS t
                    FROM (
                      SELECT DISTINCT ON (symbol, bar_time)
                        symbol, bar_time
                      FROM stock_day
                      WHERE symbol = %s
                        AND bar_time >= to_timestamp(%s)::date
                        AND bar_time <= to_timestamp(%s)::date
                      ORDER BY symbol, bar_time ASC,
                        CASE COALESCE(source, 'ib')
                          WHEN 'ib' THEN 0 WHEN 'tv' THEN 1 WHEN 'massive' THEN 2 ELSE 3 END ASC
                    ) d
                    ORDER BY bar_time ASC
                    """,
                    (sym, float(start_ts), float(end_ts)),
                )
            else:
                cur.execute(
                    """
                    SELECT extract(epoch from bar_time) AS t
                    FROM (
                      SELECT DISTINCT ON (symbol, period, bar_time)
                        symbol, period, bar_time
                      FROM stock_min
                      WHERE symbol = %s AND period = %s
                        AND bar_time >= to_timestamp(%s)
                        AND bar_time <= to_timestamp(%s)
                      ORDER BY symbol, period, bar_time ASC,
                        CASE COALESCE(source, 'ib')
                          WHEN 'ib' THEN 0 WHEN 'tv' THEN 1 WHEN 'massive' THEN 2 ELSE 3 END ASC
                    ) m
                    ORDER BY bar_time ASC
                    """,
                    (sym, per, float(start_ts), float(end_ts)),
                )
            rows = cur.fetchall()
        return [float(row[0]) for row in rows if row and row[0] is not None]
    except Exception as e:
        logger.debug("get_bar_times_in_range failed: %s", e)
        return []


def get_bars_benchmark(
    conn: Any,
    symbols: Optional[List[str]] = None,
    on_or_before: Optional[date] = None,
) -> Dict[str, Dict[str, Any]]:
    """Return latest daily bar on or before given date per symbol from stock_day."""
    sym_list = list({(s or "").strip() for s in (symbols or []) if (s or "").strip()})
    if not sym_list:
        return {}
    ref = on_or_before if on_or_before is not None else date.today()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                WITH dedup AS (
                  SELECT DISTINCT ON (symbol, bar_time)
                    symbol, bar_time, close
                  FROM stock_day
                  WHERE symbol = ANY(%s) AND bar_time <= %s
                  ORDER BY symbol, bar_time ASC,
                    CASE COALESCE(source, 'ib')
                      WHEN 'ib' THEN 0 WHEN 'tv' THEN 1 WHEN 'massive' THEN 2 ELSE 3 END ASC
                ),
                ordered AS (
                    SELECT symbol, bar_time, close,
                           LEAD(close) OVER (PARTITION BY symbol ORDER BY bar_time DESC) AS prev_close
                    FROM dedup
                )
                SELECT DISTINCT ON (symbol) symbol,
                       extract(epoch from bar_time) AS bar_time,
                       close,
                       prev_close
                FROM ordered
                ORDER BY symbol, bar_time DESC
                """,
                (sym_list, ref),
            )
            rows = cur.fetchall()
        return {
            (r["symbol"] or "").strip(): {
                "bar_time": float(r["bar_time"]) if r.get("bar_time") is not None else 0,
                "close": float(r["close"]) if r.get("close") is not None else 0,
                "prev_close": float(r["prev_close"]) if r.get("prev_close") is not None and r["prev_close"] is not None else None,
            }
            for r in rows
            if (r.get("symbol") or "").strip()
        }
    except Exception as e:
        logger.debug("get_bars_benchmark failed: %s", e)
        return {}


def get_stock_day_fallback_price(conn: Any, symbol: str) -> Optional[Tuple[float, float, Optional[float]]]:
    """Return (close, bar_time_epoch, prev_close) from stock_day for display when live quote is missing or stale.

    When the latest bar is **today**'s session date: before NY regular close (+ grace) we still use the prior
    completed session (avoids intraday partial aggregates). After that cutoff we use today's close so Positions
    can show the finalized daily bar when IB live is unavailable.
    """
    if not (symbol or "").strip():
        return None
    sym = (symbol or "").strip()
    today = date.today()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT bar_time, close,
                       extract(epoch from bar_time) AS bar_time_epoch
                FROM (
                  SELECT DISTINCT ON (bar_time)
                    bar_time, close
                  FROM stock_day
                  WHERE UPPER(TRIM(symbol)) = UPPER(TRIM(%s))
                  ORDER BY bar_time DESC,
                    CASE COALESCE(source, 'ib')
                      WHEN 'ib' THEN 0 WHEN 'tv' THEN 1 WHEN 'massive' THEN 2 ELSE 3 END ASC
                ) d
                ORDER BY bar_time DESC
                LIMIT 3
                """,
                (sym,),
            )
            rows = cur.fetchall()
        if not rows:
            return None
        bt0 = rows[0].get("bar_time")
        bar_date_0 = bt0.date() if hasattr(bt0, "date") else (bt0 if isinstance(bt0, date) else today)
        if bar_date_0 == today:
            try:
                from src.massive.stock_ohlc_daily_smart import is_ny_session_safely_closed

                session_done = is_ny_session_safely_closed()
            except Exception:
                session_done = False
            if session_done:
                r = rows[0]
                prev_close = rows[1].get("close") if len(rows) > 1 else None
            else:
                if len(rows) < 2:
                    return None
                r = rows[1]
                prev_close = rows[2].get("close") if len(rows) > 2 else None
        else:
            r = rows[0]
            prev_close = rows[1].get("close") if len(rows) > 1 else None
        close = r.get("close")
        ts = r.get("bar_time_epoch")
        if close is None or ts is None:
            return None
        try:
            c = float(close)
            t = float(ts)
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
        except (TypeError, ValueError):
            pass
        return None
    except Exception as e:
        logger.debug("get_stock_day_fallback_price failed: %s", e)
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
                """
                SELECT contract_key, symbol, sec_type, expiry, strike, option_right, bid, ask, last, mid,
                       extract(epoch from updated_at) AS ts
                FROM contract_quote_live
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
    """Return row counts for the given symbol in stock_day and stock_min (per period)."""
    if not symbol or not symbol.strip():
        return {"stock_day": 0, "stock_min": {}}
    sym = symbol.strip()
    out: Dict[str, Any] = {"stock_day": 0, "stock_min": {}}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stock_day WHERE symbol = %s", (sym,))
            row = cur.fetchone()
            out["stock_day"] = int(row[0]) if row and row[0] is not None else 0
            for per in ("1 min", "5 mins", "1 hour"):
                cur.execute(
                    "SELECT COUNT(*) FROM stock_min WHERE symbol = %s AND period = %s",
                    (sym, per),
                )
                r = cur.fetchone()
                out["stock_min"][per] = int(r[0]) if r and r[0] is not None else 0
        return out
    except Exception as e:
        logger.debug("get_bars_stats failed: %s", e)
        return {"stock_day": 0, "stock_min": {}}


def _coverage_day_iso(v: Any) -> Optional[str]:
    """Normalize MIN/MAX(bar_time) for JSON: always YYYY-MM-DD string."""
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
    """Symbols starting with ``^`` that appear in ``stock_day`` or ``stock_min`` (e.g. ``^VIX``) for coverage lists."""
    out: set[str] = set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT symbol FROM stock_day
                WHERE replace(symbol, U&'\\FF3E', '^') LIKE '^%'
                UNION
                SELECT DISTINCT symbol FROM stock_min
                WHERE replace(symbol, U&'\\FF3E', '^') LIKE '^%'
                """,
            )
            for row in cur.fetchall() or []:
                s = (row[0] or "").strip()
                if s:
                    out.add(s)
        return sorted(out)
    except Exception as e:
        logger.debug("distinct_caret_symbols_in_stock_bars_tables failed: %s", e)
        return []


def get_bars_coverage(conn: Any, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Return per-symbol coverage (count, min_ts, max_ts) for stock_day and stock_min.

    Matches ``stock_* .symbol`` using ``upper(trim(symbol))`` so config ``^VIX`` aligns with DB
    even when casing or surrounding whitespace differs.
    """
    sym_list = _ordered_unique_symbols(list(symbols) if symbols else None)
    if not sym_list:
        return []
    norm_list = list(dict.fromkeys(_norm_bars_symbol(s) for s in sym_list))
    empty_day = {"count": 0, "min_day": None, "max_day": None, "min_ts": None, "max_ts": None}
    out: List[Dict[str, Any]] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT upper(trim(replace(symbol, U&'\\FF3E', '^'))) AS sym_norm,
                       COUNT(*) AS cnt,
                       MIN(bar_time)::text AS min_day,
                       MAX(bar_time)::text AS max_day,
                       extract(epoch from MIN(bar_time)) AS min_ts,
                       extract(epoch from MAX(bar_time)) AS max_ts
                FROM stock_day
                WHERE upper(trim(replace(symbol, U&'\\FF3E', '^'))) = ANY(%s)
                GROUP BY upper(trim(replace(symbol, U&'\\FF3E', '^')))
                """,
                (norm_list,),
            )
            day_rows: Dict[str, Dict[str, Any]] = {}
            for row in cur.fetchall():
                sn = _norm_bars_symbol(row[0] or "")
                if not sn:
                    continue
                day_rows[sn] = {
                    "count": int(row[1]),
                    "min_day": _coverage_day_iso(row[2]),
                    "max_day": _coverage_day_iso(row[3]),
                    "min_ts": float(row[4]) if row[4] is not None else None,
                    "max_ts": float(row[5]) if row[5] is not None else None,
                }
            cur.execute(
                """
                SELECT upper(trim(replace(symbol, U&'\\FF3E', '^'))) AS sym_norm, period,
                       COUNT(*) AS cnt,
                       extract(epoch from MIN(bar_time)) AS min_ts,
                       extract(epoch from MAX(bar_time)) AS max_ts
                FROM stock_min
                WHERE upper(trim(replace(symbol, U&'\\FF3E', '^'))) = ANY(%s) AND period IN ('1 min', '5 mins', '1 hour')
                GROUP BY upper(trim(replace(symbol, U&'\\FF3E', '^'))), period
                """,
                (norm_list,),
            )
            min_rows: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for row in cur.fetchall():
                sn = _norm_bars_symbol(row[0] or "")
                per = row[1]
                cnt, min_ts, max_ts = int(row[2]), row[3], row[4]
                if not sn:
                    continue
                if sn not in min_rows:
                    min_rows[sn] = {}
                min_rows[sn][per] = {
                    "count": cnt,
                    "min_ts": float(min_ts) if min_ts is not None else None,
                    "max_ts": float(max_ts) if max_ts is not None else None,
                }
            for sym in sym_list:
                n = _norm_bars_symbol(sym)
                day = day_rows.get(n, dict(empty_day))
                mins = min_rows.get(n, {})
                out.append({
                    "symbol": sym,
                    "stock_day": day,
                    "stock_min": {
                        "1 min": mins.get("1 min", {"count": 0, "min_ts": None, "max_ts": None}),
                        "5 mins": mins.get("5 mins", {"count": 0, "min_ts": None, "max_ts": None}),
                        "1 hour": mins.get("1 hour", {"count": 0, "min_ts": None, "max_ts": None}),
                    },
                })
        return out
    except Exception as e:
        logger.debug("get_bars_coverage failed: %s", e)
        return []


# ----- Module-level (status_config) for re-export -----

def write_ohlc_bars_to_db(status_config: dict, rows: List[Dict[str, Any]]) -> bool:
    """Write OHLC bars to stock_day (1 D) or stock_min. UPSERT by (symbol, bar_time) or (symbol, period, bar_time)."""
    if not rows or not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
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
                    open_ = r.get("open")
                    high = r.get("high")
                    low = r.get("low")
                    close = r.get("close")
                    volume = r.get("volume")
                    if period.upper() == "1 D":
                        # stock_day.bar_time is DATE — use original local date to
                        # avoid UTC date shift (e.g. 18:00-0600 → next day in UTC)
                        bar_date_str = r.get("bar_date")
                        if bar_date_str:
                            try:
                                bar_d = date.fromisoformat(str(bar_date_str)[:10])
                            except (ValueError, TypeError):
                                bar_d = bar_dt.date() if isinstance(bar_dt, datetime) else bar_dt
                        elif isinstance(bar_dt, datetime):
                            bar_d = bar_dt.date()
                        else:
                            bar_d = bar_dt
                        cur.execute(
                            """
                            INSERT INTO stock_day (symbol, bar_time, open, high, low, close, volume, source)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 'ib')
                            ON CONFLICT (symbol, bar_time, source)
                            DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                                          close = EXCLUDED.close, volume = EXCLUDED.volume
                            """,
                            (symbol, bar_d, open_, high, low, close, volume),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO stock_min (symbol, period, bar_time, open, high, low, close, volume, source)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ib')
                            ON CONFLICT (symbol, period, bar_time, source)
                            DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                                          close = EXCLUDED.close, volume = EXCLUDED.volume
                            """,
                            (symbol, period, bar_dt, open_, high, low, close, volume),
                        )
            conn.commit()
            logger.info("[R-A3] write_ohlc_bars_to_db: wrote %s rows to stock_day/stock_min", len(rows))
            return True
        finally:
            conn.close()
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
    """Delete stock_day and/or stock_min rows for the given symbol. Returns {ok, deleted_day, deleted_min} or {ok: False, error}."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return {"ok": False, "error": "No postgres config"}
    sym = (symbol or "").strip()
    if not sym:
        return {"ok": False, "error": "Symbol required"}
    valid_periods = {"1 D", "1 min", "5 mins", "1 hour"}
    if periods:
        periods = [p.strip() for p in periods if (p or "").strip() in valid_periods]
    delete_day = not periods or "1 D" in periods
    min_periods = [p for p in ("1 min", "5 mins", "1 hour") if not periods or p in periods]
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                deleted_day = 0
                deleted_min = 0
                if delete_day:
                    cur.execute("DELETE FROM stock_day WHERE symbol = %s", (sym,))
                    deleted_day = cur.rowcount
                if min_periods:
                    cur.execute(
                        "DELETE FROM stock_min WHERE symbol = %s AND period = ANY(%s)",
                        (sym, min_periods),
                    )
                    deleted_min = cur.rowcount
            conn.commit()
            logger.info("delete_stock_bars_for_symbol %s periods=%s: deleted_day=%s deleted_min=%s", sym, periods, deleted_day, deleted_min)
            return {"ok": True, "deleted_day": deleted_day, "deleted_min": deleted_min}
        finally:
            conn.close()
    except Exception as e:
        logger.warning("delete_stock_bars_for_symbol failed: %s", e)
        return {"ok": False, "error": str(e)}


def insert_job_bars_backfill(
    status_config: dict,
    symbol: str,
    period: str,
    years: Optional[float] = None,
    days: Optional[int] = None,
    override_days: Optional[float] = None,
    span_hours: Optional[float] = None,
    skip_ib: bool = False,
    api_interval_sec: int = 10,
) -> Optional[int]:
    """Insert a pending job_bars_backfill row. Returns job_bars_backfill_id or None on failure."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO job_bars_backfill (symbol, period, years, days, override_days, span_hours, skip_ib, api_interval_sec, status, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', now(), now())
                    RETURNING job_bars_backfill_id
                    """,
                    (
                        (symbol or "").strip(),
                        (period or "1 D").strip(),
                        years,
                        days,
                        override_days,
                        span_hours,
                        bool(skip_ib),
                        max(0, min(300, int(api_interval_sec))),
                    ),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("insert_job_bars_backfill failed: %s", e)
        return None


def get_job_bars_backfill_list(
    status_config: dict,
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Return job_bars_backfill rows (newest first) with optional status filter and pagination. Returns (rows, total_count)."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return [], 0
    try:
        try:
            limit = max(1, min(500, int(limit))) if limit is not None else 50
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(offset)) if offset is not None else 0
        except (TypeError, ValueError):
            offset = 0
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                st = status.strip().lower() if status and status.strip() else None
                if st and st not in ("pending", "running", "done", "failed"):
                    st = None
                where = "WHERE status = %s" if st else ""
                args_count = [st] if st else []
                cur.execute(
                    f"SELECT COUNT(*) AS count FROM job_bars_backfill {where}",
                    args_count,
                )
                total = int(cur.fetchone()["count"])
                args_list = (args_count + [limit, offset]) if st else [limit, offset]
                cur.execute(
                    f"""
                    SELECT job_bars_backfill_id, symbol, period, years, days, override_days, span_hours, skip_ib, api_interval_sec, status, result,
                           created_at, updated_at
                    FROM job_bars_backfill
                    {where}
                    ORDER BY job_bars_backfill_id DESC
                    LIMIT %s OFFSET %s
                    """,
                    args_list,
                )
                rows = cur.fetchall()
            return [dict(r) for r in rows] if rows else [], total
        finally:
            conn.close()
    except Exception as e:
        logger.warning("get_job_bars_backfill_list failed: %s", e)
        raise


def count_job_bars_backfill_by_status(status_config: dict) -> Dict[str, int]:
    """Return counts per status for the full bars backfill table."""
    labels = ("pending", "running", "done", "failed")
    out: Dict[str, int] = {s: 0 for s in labels}
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return out
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*)::bigint FROM job_bars_backfill GROUP BY status",
                )
                for row in cur.fetchall() or []:
                    st = str(row[0] or "").strip().lower()
                    if st in out:
                        out[st] = int(row[1])
            return out
        finally:
            conn.close()
    except Exception as e:
        logger.warning("count_job_bars_backfill_by_status failed: %s", e)
        return out


def delete_job_bars_backfill(status_config: dict, job_id: Any) -> bool:
    """Delete one job_bars_backfill row by job_bars_backfill_id. Returns True if deleted (or not found)."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM job_bars_backfill WHERE job_bars_backfill_id = %s", (jid,))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("delete_job_bars_backfill failed: %s", e)
        return False


def delete_all_job_bars_backfill(status_config: dict, status_filter: Optional[str] = None) -> int:
    """Delete all job_bars_backfill rows, optionally only those with given status. Returns number deleted."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return 0
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                if status_filter and status_filter.strip().lower() in ("pending", "running", "done", "failed"):
                    cur.execute("DELETE FROM job_bars_backfill WHERE status = %s", (status_filter.strip().lower(),))
                else:
                    cur.execute("DELETE FROM job_bars_backfill")
                deleted = cur.rowcount
            conn.commit()
            return deleted
        finally:
            conn.close()
    except Exception as e:
        logger.warning("delete_all_job_bars_backfill failed: %s", e)
        return 0


def get_job_bars_backfill(status_config: dict, job_id: Any) -> Optional[Dict[str, Any]]:
    """Return one job_bars_backfill row by job_bars_backfill_id, or None."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return None
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_bars_backfill_id, symbol, period, years, days, override_days, span_hours, skip_ib, api_interval_sec, status, result,
                           created_at, updated_at
                    FROM job_bars_backfill
                    WHERE job_bars_backfill_id = %s
                    """,
                    (jid,),
                )
                row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("get_job_bars_backfill failed: %s", e)
        return None


def claim_next_pending_job_bars_backfill(status_config: dict) -> Optional[Dict[str, Any]]:
    """Select one pending job_bars_backfill row with FOR UPDATE SKIP LOCKED, set status=running, return job row."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_bars_backfill_id, symbol, period, years, days, override_days
                    FROM job_bars_backfill
                    WHERE status = 'pending'
                    ORDER BY job_bars_backfill_id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cur.fetchone()
                if not row:
                    return None
                jid = row["job_bars_backfill_id"]
                cur.execute(
                    """
                    UPDATE job_bars_backfill
                    SET status = 'running', updated_at = now()
                    WHERE job_bars_backfill_id = %s
                    """,
                    (jid,),
                )
            conn.commit()
            return dict(row)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("claim_next_pending_job_bars_backfill failed: %s", e)
        return None


def update_job_bars_backfill_result(
    status_config: dict,
    job_id: int,
    status: str,
    result: Optional[Dict[str, Any]] = None,
) -> bool:
    """Set job status and result (done/failed). Returns True on success."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_bars_backfill
                    SET status = %s, result = %s, updated_at = now()
                    WHERE job_bars_backfill_id = %s
                    """,
                    (status, json.dumps(result) if result is not None else None, job_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("update_job_bars_backfill_result failed: %s", e)
        return False


_JOB_BARS_BACKFILL_SELECT_COLS = (
    "job_bars_backfill_id, symbol, period, years, days, override_days, span_hours, skip_ib, api_interval_sec, "
    "status, result, created_at, updated_at"
)

_JOB_BARS_BACKFILL_RETURNING_J = (
    "j.job_bars_backfill_id, j.symbol, j.period, j.years, j.days, j.override_days, j.span_hours, j.skip_ib, "
    "j.api_interval_sec, j.status, j.result, j.created_at, j.updated_at"
)


def reset_failed_job_bars_backfill_to_pending(status_config: dict, job_id: Any) -> Optional[Dict[str, Any]]:
    """If the row is ``failed``, set ``pending`` and clear ``result``. Returns the row dict for re-enqueue, else None."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        jid = int(job_id)
    except (TypeError, ValueError):
        return None
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    UPDATE job_bars_backfill
                    SET status = 'pending', result = NULL, updated_at = now()
                    WHERE job_bars_backfill_id = %s AND status = 'failed'
                    RETURNING {_JOB_BARS_BACKFILL_SELECT_COLS}
                    """,
                    (jid,),
                )
                row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("reset_failed_job_bars_backfill_to_pending failed: %s", e)
        return None


def reset_failed_jobs_bars_backfill_to_pending_batch(
    status_config: dict,
    limit: int,
) -> List[Dict[str, Any]]:
    """Reset up to ``limit`` oldest failed jobs to pending (clears result). Returns rows for Celery re-enqueue."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return []
    try:
        lim = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        lim = 100
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    WITH cte AS (
                        SELECT job_bars_backfill_id
                        FROM job_bars_backfill
                        WHERE status = 'failed'
                        ORDER BY job_bars_backfill_id ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE job_bars_backfill j
                    SET status = 'pending', result = NULL, updated_at = now()
                    FROM cte
                    WHERE j.job_bars_backfill_id = cte.job_bars_backfill_id
                    RETURNING {_JOB_BARS_BACKFILL_RETURNING_J}
                    """,
                    (lim,),
                )
                # RETURNING list: prefix each column with j. except first replacement is wrong — use explicit list
                rows = cur.fetchall()
            conn.commit()
            return [dict(r) for r in rows] if rows else []
        finally:
            conn.close()
    except Exception as e:
        logger.warning("reset_failed_jobs_bars_backfill_to_pending_batch failed: %s", e)
        return []


def trim_job_bars_backfill(status_config: dict, keep: int = 200) -> int:
    """Keep only the most recent keep jobs; delete older ones. Returns number of rows deleted."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return 0
    k = max(1, min(int(keep), 50_000))
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH kept AS (SELECT job_bars_backfill_id FROM job_bars_backfill ORDER BY job_bars_backfill_id DESC LIMIT %s)
                    DELETE FROM job_bars_backfill WHERE job_bars_backfill_id NOT IN (SELECT job_bars_backfill_id FROM kept)
                    """,
                    (k,),
                )
                deleted = cur.rowcount
            conn.commit()
            return int(deleted)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("trim_job_bars_backfill failed: %s", e)
        return 0


def get_job_bars_backfill_last_updated(status_config: dict) -> Optional[float]:
    """Return max(updated_at) from job_bars_backfill as Unix timestamp, or None."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXTRACT(EPOCH FROM max(updated_at))::double precision FROM job_bars_backfill"
                )
                row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()
    except Exception as e:
        logger.debug("get_job_bars_backfill_last_updated failed: %s", e)
        return None


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
    """Return list of holidays from reference_us_holidays. exchange=None returns all exchanges."""
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


def add_market_holiday(
    status_config: dict, date_str: str, label: Optional[str] = None, exchange: str = "NYSE"
) -> bool:
    """Insert one row into reference_us_holidays. date_str YYYY-MM-DD. Returns True on success."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            return add_market_holiday_conn(conn, date_str, label=label, exchange=exchange)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("add_market_holiday failed: %s", e)
        return False


def delete_market_holiday(status_config: dict, date_str: str, exchange: str = "NYSE") -> bool:
    """Delete one row from reference_us_holidays. Returns True on success."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            return delete_market_holiday_conn(conn, date_str, exchange=exchange)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("delete_market_holiday failed: %s", e)
        return False


__all__ = [
    "write_ohlc_bars_to_db",
    "write_stock_bars",
    "delete_stock_bars_for_symbol",
    "insert_job_bars_backfill",
    "get_job_bars_backfill_list",
    "get_job_bars_backfill",
    "delete_job_bars_backfill",
    "delete_all_job_bars_backfill",
    "claim_next_pending_job_bars_backfill",
    "update_job_bars_backfill_result",
    "reset_failed_job_bars_backfill_to_pending",
    "reset_failed_jobs_bars_backfill_to_pending_batch",
    "trim_job_bars_backfill",
    "get_job_bars_backfill_last_updated",
    "get_is_us_trading_day",
    "get_market_holidays",
    "add_market_holiday",
    "delete_market_holiday",
]
