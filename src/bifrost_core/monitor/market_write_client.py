"""HTTP write client for Plugin Market Data API (ingest job enqueue).

Used by monitor/integrations/index_data_client.py. monitor/services/market_jobs.py (the bars
backfill enqueue) went with its last caller, api's market-data routes, in core 0.46.0 (TD-80).
The bars ingest / delete calls went with their only callers, monitor.reader's
write_ohlc_bars_to_db / write_stock_bars / delete_stock_bars_for_symbol, in core 0.34.0
(TD-78): nothing in api, worker or Flex called them.

Pattern mirrors bifrost-trade-api/research/market_data_client.py (urllib only, no new deps).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict

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


def post_ingest_enqueue(
    kind: str,
    payload: Dict[str, Any] | None = None,
    *,
    priority: int = 0,
    timeout: int = 30,
) -> Dict[str, Any]:
    """POST /ingest/enqueue. Returns Plugin job dict (ok, job_id, kind, …)."""
    url = f"{_plugin_base_url()}/ingest/enqueue"
    body = json.dumps({"kind": kind, "payload": payload or {}, "priority": priority}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    for k, v in _write_headers().items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())
