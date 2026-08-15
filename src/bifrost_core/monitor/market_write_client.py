"""HTTP write client for Plugin Market Data API (bars ingest + delete).

Used by monitor/reader/market.py write functions to POST/DELETE bars
via the Plugin API instead of direct psycopg2 SQL.

Pattern mirrors bifrost-trade-api/research/market_data_client.py (urllib only, no new deps).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _plugin_base_url() -> str:
    return os.environ.get("MARKET_DATA_PLUGIN_URL", "http://localhost:8790/market")


def _write_headers(*, content_type: bool = True) -> dict[str, str]:
    headers: dict[str, str] = {}
    if content_type:
        headers["Content-Type"] = "application/json"
    token = (
        os.environ.get("MARKET_DATA_WRITE_TOKEN", "").strip()
        or os.environ.get("PLUGIN_OPERATOR_TOKEN", "").strip()
        or os.environ.get("PLATFORM_OPERATOR_TOKEN", "").strip()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def post_bars_ingest(rows: List[Dict[str, Any]], timeout: int = 60) -> Dict[str, Any]:
    """POST rows to /stocks/bars/ingest. Returns {"ok": True, "written": N} on success."""
    url = f"{_plugin_base_url()}/stocks/bars/ingest"
    body = json.dumps({"rows": rows}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in _write_headers().items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def delete_bars(
    symbol: str,
    delete_daily: bool = True,
    periods: List[str] | None = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """DELETE /stocks/bars?symbol=...&delete_daily=...&periods=... Returns response dict."""
    base = _plugin_base_url()
    params: Dict[str, str] = {
        "symbol": symbol,
        "delete_daily": "true" if delete_daily else "false",
    }
    if periods:
        params["periods"] = ",".join(periods)
    qs = "&".join(
        f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items()
    )
    url = f"{base}/stocks/bars?{qs}"
    req = urllib.request.Request(url, method="DELETE")
    for k, v in _write_headers(content_type=False).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())
