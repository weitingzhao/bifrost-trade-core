"""Upsert stock OHLC from Massive/Polygon REST into stock_day / stock_min (source='massive')."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

SOURCE_MASSIVE = "massive"


def _pg_row_first_value(row: Any) -> Any:
    """First cell from a fetchone row (tuple or mapping / RealDictRow)."""
    if row is None:
        return None
    if isinstance(row, dict):
        vals = list(row.values())
        return vals[0] if vals else None
    try:
        return row[0]
    except (IndexError, TypeError, KeyError):
        return None


def get_massive_stock_day_max_date(cur: Any, symbol: str) -> Optional[date]:
    """Latest daily bar date in stock_day for symbol (Massive source only)."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    cur.execute(
        """
        SELECT MAX(bar_time) FROM stock_day
        WHERE symbol = %s AND source = %s
        """,
        (sym, SOURCE_MASSIVE),
    )
    row = cur.fetchone()
    v = _pg_row_first_value(row)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def timespan_to_stock_period(timespan: str, multiplier: int = 1) -> str:
    """Map Massive timespan + multiplier to stock_min.period label."""
    ts = (timespan or "minute").strip().lower()
    m = max(1, int(multiplier or 1))
    if ts == "minute":
        if m == 5:
            return "5 mins"
        return f"{m} min" if m > 1 else "1 min"
    if ts == "hour":
        return f"{m} hour" if m > 1 else "1 hour"
    if ts == "second":
        return f"{m} sec" if m > 1 else "1 sec"
    if ts == "day":
        return f"{m} D" if m > 1 else "1 D"
    if ts == "week":
        return f"{m} W" if m > 1 else "1 W"
    if ts == "month":
        return f"{m} M" if m > 1 else "1 M"
    return f"{m} {ts}"


def apply_stock_custom_bars(
    cur: Any,
    ticker: str,
    timespan: str,
    multiplier: int,
    aggs: Dict[str, Any],
    *,
    adjusted: Optional[bool] = None,
) -> int:
    """Upsert bars from GET /v2/aggs/ticker/.../range. Returns rows written."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return 0
    bars = aggs.get("results") or []
    if not isinstance(bars, list):
        return 0
    ts = (timespan or "minute").strip().lower()
    mult = max(1, int(multiplier or 1))
    n = 0
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        t = bar.get("t")
        if t is None:
            continue
        try:
            ts_ms = int(t)
            bt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        o = bar.get("o")
        h = bar.get("h")
        l_ = bar.get("l")
        c = bar.get("c")
        v = bar.get("v")
        vw = bar.get("vw")
        tc = bar.get("n")
        extras: Dict[str, Any] = {}
        if bar.get("otc") is not None:
            extras["otc"] = bar.get("otc")
        extras_js = json.dumps(extras) if extras else None

        if ts in ("day", "week", "month"):
            bar_d = bt.date()
            cur.execute(
                """
                INSERT INTO stock_day (
                  symbol, bar_time, open, high, low, close, volume,
                  source, vwap, trade_count, adjusted, extras, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (symbol, bar_time, source)
                DO UPDATE SET
                  open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                  close = EXCLUDED.close, volume = EXCLUDED.volume,
                  vwap = EXCLUDED.vwap, trade_count = EXCLUDED.trade_count,
                  adjusted = COALESCE(EXCLUDED.adjusted, stock_day.adjusted),
                  extras = COALESCE(EXCLUDED.extras, stock_day.extras)
                """,
                (
                    sym,
                    bar_d,
                    float(o) if o is not None else None,
                    float(h) if h is not None else None,
                    float(l_) if l_ is not None else None,
                    float(c) if c is not None else None,
                    float(v) if v is not None else None,
                    SOURCE_MASSIVE,
                    float(vw) if vw is not None else None,
                    int(tc) if tc is not None else None,
                    adjusted,
                    extras_js,
                ),
            )
        else:
            period = timespan_to_stock_period(timespan, mult)
            cur.execute(
                """
                INSERT INTO stock_min (
                  symbol, period, bar_time, open, high, low, close, volume,
                  source, vwap, trade_count, adjusted, extras, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (symbol, period, bar_time, source)
                DO UPDATE SET
                  open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                  close = EXCLUDED.close, volume = EXCLUDED.volume,
                  vwap = EXCLUDED.vwap, trade_count = EXCLUDED.trade_count,
                  adjusted = COALESCE(EXCLUDED.adjusted, stock_min.adjusted),
                  extras = COALESCE(EXCLUDED.extras, stock_min.extras)
                """,
                (
                    sym,
                    period,
                    bt,
                    float(o) if o is not None else None,
                    float(h) if h is not None else None,
                    float(l_) if l_ is not None else None,
                    float(c) if c is not None else None,
                    float(v) if v is not None else None,
                    SOURCE_MASSIVE,
                    float(vw) if vw is not None else None,
                    int(tc) if tc is not None else None,
                    adjusted,
                    extras_js,
                ),
            )
        n += 1
    return n


def apply_stock_grouped_daily(
    cur: Any,
    trade_date: str,
    data: Dict[str, Any],
    *,
    adjusted: Optional[bool] = None,
) -> int:
    """Upsert all symbols from GET .../market/stocks/{date}."""
    raw = trade_date.strip()[:10]
    try:
        bar_d = date.fromisoformat(raw)
    except (ValueError, TypeError):
        return 0
    results = data.get("results") or []
    if not isinstance(results, list):
        return 0
    n = 0
    for row in results:
        if not isinstance(row, dict):
            continue
        sym = (row.get("T") or "").strip().upper()
        if not sym:
            continue
        o = row.get("o")
        h = row.get("h")
        l_ = row.get("l")
        c = row.get("c")
        v = row.get("v")
        vw = row.get("vw")
        tc = row.get("n")
        extras: Dict[str, Any] = {}
        if row.get("otc"):
            extras["otc"] = True
        extras_js = json.dumps(extras) if extras else None
        cur.execute(
            """
            INSERT INTO stock_day (
              symbol, bar_time, open, high, low, close, volume,
              source, vwap, trade_count, adjusted, extras, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (symbol, bar_time, source)
            DO UPDATE SET
              open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
              close = EXCLUDED.close, volume = EXCLUDED.volume,
              vwap = EXCLUDED.vwap, trade_count = EXCLUDED.trade_count,
              adjusted = COALESCE(EXCLUDED.adjusted, stock_day.adjusted),
              extras = COALESCE(EXCLUDED.extras, stock_day.extras)
            """,
            (
                sym,
                bar_d,
                float(o) if o is not None else None,
                float(h) if h is not None else None,
                float(l_) if l_ is not None else None,
                float(c) if c is not None else None,
                float(v) if v is not None else None,
                SOURCE_MASSIVE,
                float(vw) if vw is not None else None,
                int(tc) if tc is not None else None,
                adjusted,
                extras_js,
            ),
        )
        n += 1
    return n


def apply_stock_daily_ticker_summary(
    cur: Any,
    ticker: str,
    data: Dict[str, Any],
    *,
    adjusted: Optional[bool] = None,
) -> int:
    """Upsert from GET /v1/open-close/{ticker}/{date}."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return 0
    raw_fd = data.get("from") or data.get("date")
    if raw_fd is None:
        from_s = ""
    elif isinstance(raw_fd, datetime):
        from_s = raw_fd.date().isoformat()[:10]
    elif isinstance(raw_fd, date):
        from_s = raw_fd.isoformat()[:10]
    else:
        from_s = str(raw_fd)[:10]
    try:
        bar_d = date.fromisoformat(from_s)
    except (ValueError, TypeError):
        return 0
    o = data.get("open")
    h = data.get("high")
    l_ = data.get("low")
    c = data.get("close")
    v = data.get("volume")
    extras = {}
    if data.get("preMarket") is not None:
        extras["preMarket"] = data.get("preMarket")
    if data.get("afterHours") is not None:
        extras["afterHours"] = data.get("afterHours")
    extras_js = json.dumps(extras) if extras else None
    cur.execute(
        """
        INSERT INTO stock_day (
          symbol, bar_time, open, high, low, close, volume,
          source, vwap, trade_count, adjusted, extras, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s, %s, now())
        ON CONFLICT (symbol, bar_time, source)
        DO UPDATE SET
          open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
          close = EXCLUDED.close, volume = EXCLUDED.volume,
          adjusted = COALESCE(EXCLUDED.adjusted, stock_day.adjusted),
          extras = COALESCE(EXCLUDED.extras, stock_day.extras)
        """,
        (
            sym,
            bar_d,
            float(o) if o is not None else None,
            float(h) if h is not None else None,
            float(l_) if l_ is not None else None,
            float(c) if c is not None else None,
            float(v) if v is not None else None,
            SOURCE_MASSIVE,
            adjusted,
            extras_js,
        ),
    )
    return 1


def apply_stock_previous_day_bar(
    cur: Any,
    ticker: str,
    data: Dict[str, Any],
    *,
    adjusted: Optional[bool] = None,
) -> int:
    """Upsert previous session bar from GET .../prev into stock_day (bar_time = session date)."""
    sym = (ticker or "").strip().upper()
    if not sym:
        return 0
    bars = data.get("results") or []
    if not isinstance(bars, list) or not bars:
        return 0
    bar = bars[0] if isinstance(bars[0], dict) else {}
    t = bar.get("t")
    if t is None:
        return 0
    try:
        ts_ms = int(t)
        bt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        bar_d = bt.date()
    except (TypeError, ValueError, OSError):
        return 0
    o = bar.get("o")
    h = bar.get("h")
    l_ = bar.get("l")
    c = bar.get("c")
    v = bar.get("v")
    vw = bar.get("vw")
    tc = bar.get("n")
    cur.execute(
        """
        INSERT INTO stock_day (
          symbol, bar_time, open, high, low, close, volume,
          source, vwap, trade_count, adjusted, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (symbol, bar_time, source)
        DO UPDATE SET
          open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
          close = EXCLUDED.close, volume = EXCLUDED.volume,
          vwap = EXCLUDED.vwap, trade_count = EXCLUDED.trade_count,
          adjusted = COALESCE(EXCLUDED.adjusted, stock_day.adjusted)
        """,
        (
            sym,
            bar_d,
            float(o) if o is not None else None,
            float(h) if h is not None else None,
            float(l_) if l_ is not None else None,
            float(c) if c is not None else None,
            float(v) if v is not None else None,
            SOURCE_MASSIVE,
            float(vw) if vw is not None else None,
            int(tc) if tc is not None else None,
            adjusted,
        ),
    )
    return 1


__all__ = [
    "SOURCE_MASSIVE",
    "get_massive_stock_day_max_date",
    "apply_stock_custom_bars",
    "apply_stock_daily_ticker_summary",
    "apply_stock_grouped_daily",
    "apply_stock_previous_day_bar",
    "timespan_to_stock_period",
]
