"""Sync public.us_equity_universe and public.sepa_symbol_price_readiness from Plugin API.

Replaces former views over market.ticker / market.stock_daily after Golden Source DROP.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MIN_BAR_ROWS = 240
_MAX_STALE_CALENDAR_DAYS = 7
_WINDOW_DAYS = 420
_PRICE_SOURCE = "polygon"


def _plugin_base_url() -> str:
    return os.environ.get("MARKET_DATA_PLUGIN_URL", "http://localhost:8790/market")


def _get_json(path: str, params: Optional[Dict[str, str]] = None, timeout: int = 120) -> Dict[str, Any]:
    base = _plugin_base_url()
    url = f"{base}{path}"
    if params:
        qs = "&".join(
            f"{k}={urllib.parse.quote(str(v))}"
            for k, v in params.items()
            if v is not None
        )
        url = f"{url}?{qs}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _parse_date(raw: Any) -> Optional[date]:
    if raw is None:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    s = str(raw)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def fetch_universe_rows() -> List[Dict[str, Any]]:
    data = _get_json("/reference/universe")
    return list(data.get("rows") or [])


def fetch_bar_aggregate(window_days: int = _WINDOW_DAYS) -> Dict[str, Dict[str, Any]]:
    data = _get_json("/readiness/bar-aggregate", {"window_days": str(window_days)})
    symbols = data.get("symbols") or {}
    return symbols if isinstance(symbols, dict) else {}


def sync_universe_from_plugin(conn: Any) -> int:
    """TRUNCATE + INSERT public.us_equity_universe from Plugin API. Returns row count."""
    rows = fetch_universe_rows()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE public.us_equity_universe")
        if rows:
            payload = [
                (
                    r.get("tickers_id"),
                    str(r.get("symbol") or "").strip().upper(),
                    r.get("name"),
                    r.get("market"),
                    r.get("locale"),
                    r.get("primary_exchange"),
                    r.get("instrument_type"),
                    r.get("active"),
                    r.get("delisted_utc"),
                    _parse_date(r.get("list_date")),
                    r.get("sector"),
                    r.get("industry"),
                )
                for r in rows
                if str(r.get("symbol") or "").strip()
            ]
            cur.executemany(
                """
                INSERT INTO public.us_equity_universe
                    (tickers_id, symbol, name, market, locale, primary_exchange,
                     instrument_type, active, delisted_utc, list_date, sector, industry)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                payload,
            )
            n = len(payload)
        else:
            n = 0
    logger.info("sync_universe_from_plugin: wrote %s rows", n)
    return n


def sync_price_readiness_from_plugin(conn: Any) -> int:
    """TRUNCATE + INSERT public.sepa_symbol_price_readiness from Plugin bar-aggregate."""
    as_of = date.today()
    stale_cutoff = as_of - timedelta(days=_MAX_STALE_CALENDAR_DAYS)
    symbols = fetch_bar_aggregate(_WINDOW_DAYS)
    payload = []
    for sym, stats in symbols.items():
        symbol = str(sym or "").strip().upper()
        if not symbol:
            continue
        bar_rows = int(stats.get("bar_rows") or 0)
        first_bar = _parse_date(stats.get("first_bar_date"))
        last_bar = _parse_date(stats.get("last_bar_date"))
        null_close = int(stats.get("null_close_rows") or 0)
        null_volume = int(stats.get("null_volume_rows") or 0)
        price_ready = (
            bar_rows >= _MIN_BAR_ROWS
            and last_bar is not None
            and last_bar >= stale_cutoff
            and null_close == 0
            and null_volume == 0
        )
        payload.append(
            (
                as_of,
                symbol,
                _PRICE_SOURCE,
                bar_rows,
                first_bar,
                last_bar,
                null_close,
                null_volume,
                price_ready,
            )
        )
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE public.sepa_symbol_price_readiness")
        if payload:
            cur.executemany(
                """
                INSERT INTO public.sepa_symbol_price_readiness
                    (as_of_date, symbol, price_source, bar_rows, first_bar_date,
                     last_bar_date, null_close_rows, null_volume_rows, price_ready)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                payload,
            )
    n = len(payload)
    logger.info("sync_price_readiness_from_plugin: wrote %s rows", n)
    return n
