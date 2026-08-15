"""HTTP write client for Plugin Market Data API (ticker reference upsert).

Used by ticker_reference.py write functions to POST ticker/overview data
via the Plugin API instead of direct psycopg2 SQL.

Pattern mirrors monitor/market_write_client.py (urllib only, no new deps).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _plugin_base_url() -> str:
    return os.environ.get("MARKET_DATA_PLUGIN_URL", "http://localhost:8790/market")


def _write_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = (
        os.environ.get("MARKET_DATA_WRITE_TOKEN", "").strip()
        or os.environ.get("PLUGIN_OPERATOR_TOKEN", "").strip()
        or os.environ.get("PLATFORM_OPERATOR_TOKEN", "").strip()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json_serializer(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def post_ticker_upsert(body: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    """POST to /reference/ticker/upsert.

    Returns ``{"ok": True, "symbol": "...", "action": "inserted"|"updated"}``.
    """
    url = f"{_plugin_base_url()}/reference/ticker/upsert"
    payload = json.dumps(body, default=_json_serializer).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    for k, v in _write_headers().items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def post_ticker_upsert_overview(body: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    """POST to /reference/ticker/upsert-overview.

    Returns ``{"ok": True, "symbol": "..."}``.
    """
    url = f"{_plugin_base_url()}/reference/ticker/upsert-overview"
    payload = json.dumps(body, default=_json_serializer).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    for k, v in _write_headers().items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())
