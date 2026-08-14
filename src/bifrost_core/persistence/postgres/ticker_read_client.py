"""HTTP read client for Plugin Market Data API (ticker reference).

Replaces direct market.ticker SQL reads in ticker_reference.py
with HTTP calls to the Plugin API /market/reference/* endpoints.

Pattern mirrors ticker_write_client.py (urllib only, no new deps).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _plugin_base_url() -> str:
    return os.environ.get("MARKET_DATA_PLUGIN_URL", "http://localhost:8790/market")


def _get_json(path: str, params: Optional[Dict[str, str]] = None, timeout: int = 30) -> Dict[str, Any]:
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


def ticker_exists(symbol: str) -> bool:
    """Check if a symbol exists in market.ticker via Plugin API."""
    try:
        data = _get_json("/reference/ticker", {"symbol": symbol})
        return data.get("ok", False)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise
    except Exception:
        return False


def search_tickers_via_plugin(q: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Search tickers via Plugin API /reference/tickers/search."""
    data = _get_json("/reference/tickers/search", {"q": q, "limit": str(limit)})
    results = data.get("results", [])
    out: List[Dict[str, Any]] = []
    for r in results:
        out.append({
            "tickers_id": None,
            "ticker": r.get("symbol"),
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "exchange": r.get("primary_exchange"),
            "primary_exchange": r.get("primary_exchange"),
            "instrument_type": r.get("instrument_type"),
            "active": r.get("active"),
        })
    return out


def fetch_ticker_detail_via_plugin(symbol: str) -> Optional[Dict[str, Any]]:
    """Single ticker from Plugin API /reference/ticker."""
    try:
        data = _get_json("/reference/ticker", {"symbol": symbol})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if not data.get("ok"):
        return None
    t = data.get("ticker", {})
    dct: Dict[str, Any] = dict(t)
    dct["ticker"] = dct.get("symbol")
    dct["currency_name"] = dct.get("currency")
    dct["exchange"] = dct.get("primary_exchange")
    dct["overview_updated_at"] = dct.get("updated_at")
    return dct


def fetch_ticker_batch_via_plugin(symbols: List[str]) -> List[Dict[str, Any]]:
    """Batch ticker lookup from Plugin API /reference/tickers/batch."""
    if not symbols:
        return []
    data = _get_json("/reference/tickers/batch", {"symbols": ",".join(symbols)})
    return data.get("tickers", [])


def overview_coverage_via_plugin() -> Dict[str, int]:
    """Coverage stats from Plugin API /reference/tickers/overview-coverage."""
    data = _get_json("/reference/tickers/overview-coverage")
    return {
        "total_tickers": data.get("total", 0),
        "filled": data.get("filled", 0),
        "missing": data.get("missing", 0),
    }


def missing_overview_via_plugin(limit: int = 500, offset: int = 0) -> List[str]:
    """Symbols missing overview from Plugin API /reference/tickers/missing-overview."""
    data = _get_json(
        "/reference/tickers/missing-overview",
        {"limit": str(limit), "offset": str(offset)},
    )
    return data.get("tickers", [])


def related_coverage_via_plugin() -> Dict[str, int]:
    """Related coverage from Plugin API /reference/tickers/related-coverage."""
    data = _get_json("/reference/tickers/related-coverage")
    return {
        "total_tickers": data.get("total", 0),
        "filled": data.get("filled", 0),
        "missing": data.get("missing", 0),
    }


def missing_related_via_plugin(limit: int = 500, offset: int = 0) -> List[str]:
    """Symbols missing related from Plugin API."""
    data = _get_json(
        "/reference/tickers/missing-related",
        {"limit": str(limit), "offset": str(offset)},
    )
    return data.get("tickers", [])


def filled_related_via_plugin(limit: int = 500, offset: int = 0) -> List[str]:
    """Symbols with related from Plugin API."""
    data = _get_json(
        "/reference/tickers/filled-related",
        {"limit": str(limit), "offset": str(offset)},
    )
    return data.get("tickers", [])


def universe_count_via_plugin() -> int:
    """Total ticker count from Plugin API."""
    data = _get_json("/reference/tickers/universe-count")
    return data.get("total_tickers", 0)


def all_ticker_symbols_via_plugin() -> List[str]:
    """All ticker symbols. Uses batch endpoint with high limit."""
    data = _get_json("/reference/tickers/search", {"q": "", "limit": "100"})
    return [r.get("symbol", "") for r in data.get("results", []) if r.get("symbol")]


def ticker_types_via_plugin(asset_class: str = "*", locale: str = "*") -> List[Dict[str, Any]]:
    """Ticker types from Plugin API."""
    data = _get_json("/reference/ticker-types", {"asset_class": asset_class, "locale": locale})
    return data.get("rows", [])


def ticker_types_count_via_plugin() -> int:
    """Ticker type count from Plugin API."""
    data = _get_json("/reference/ticker-types/count")
    return data.get("total_ticker_types", 0)


def ticker_related_via_plugin(symbol: str) -> List[Dict[str, Any]]:
    """Related tickers from Plugin API."""
    try:
        data = _get_json(f"/reference/tickers/{urllib.parse.quote(symbol)}/related")
    except urllib.error.HTTPError:
        return []
    return data.get("related", [])
