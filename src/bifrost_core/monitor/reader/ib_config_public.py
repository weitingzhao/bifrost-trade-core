"""Public JSON for Monitor GET /status ``config`` (``ib_client`` + ``ib_flex``).

``ib_client``: ``client`` (host/TCP ports), ``port`` (IB API client IDs), ``account`` (stream/trading
account IDs from DB), ``timeout_sec``.

``ib_flex``: default/init range days + Flex tokens/rows (former ``flex_config``).

Internal merged dict from ``StatusReader.get_ib_config()`` still uses YAML/DB keys; transform at HTTP boundary only.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _int_merge(m: Dict[str, Any], *keys: str, default: int = 0) -> int:
    for k in keys:
        v = m.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return default


def ib_client_for_api(merged: Dict[str, Any]) -> Dict[str, Any]:
    """Map internal merged IB dict to API-facing ``ib_client``."""
    m = merged or {}
    host = str(m.get("host") or m.get("ib_host") or "").strip() or "127.0.0.1"
    ib2_raw = m.get("ib2_host")
    ib2 = str(ib2_raw).strip() if ib2_raw else ""

    ptp = str(m.get("port_type") or m.get("ib_port_type") or "tws_paper").strip().lower()
    ib2_ptp = m.get("ib2_port_type")
    ib2_ptp_s = str(ib2_ptp).strip().lower() if ib2_ptp else None

    port = m.get("port")
    if port is None:
        port = m.get("ib_port")
    ib2_port = m.get("ib2_port")

    ct = m.get("connect_timeout")
    try:
        timeout_sec = float(ct) if ct is not None else 60.0
    except (TypeError, ValueError):
        timeout_sec = 60.0

    return {
        "client": {
            "host_ip": host,
            "host_port_type": ptp,
            "host_port": int(port) if port is not None else None,
            "secondary_host_ip": ib2 or None,
            "secondary_port_type": ib2_ptp_s,
            "secondary_port": int(ib2_port) if ib2_port is not None else None,
        },
        "port": {
            "trading": _int_merge(m, "client_id_daemon", "ib_client_id_daemon", default=1),
            "listener_host": _int_merge(m, "client_id_listener", "ib_client_id_listener", default=2),
            "listener_secondary": _int_merge(m, "ib2_client_id_listener", default=3),
            "operator_host": _int_merge(m, "client_id_operator", "ib_client_id_operator", default=100),
            "operator_secondary": _int_merge(m, "ib2_client_id_operator", default=102),
            "ingestor": _int_merge(m, "client_id_ib_ingestor", "ib_client_id_ib_ingestor", default=150),
            "account_agent": _int_merge(
                m, "client_id_account_agent", "ib_client_id_account_agent", default=151
            ),
            "account_agent_secondary": _int_merge(
                m, "ib2_client_id_account_agent", default=152
            ),
            "market_data_worker": _int_merge(
                m, "client_id_worker_market", "ib_client_id_worker_market", default=500
            ),
        },
        "account": {
            "trading": m.get("ib_host_account_id"),
            "event_host": m.get("stream_host_account_id"),
            "event_secondary": m.get("stream_secondary_account_id"),
        },
        "timeout_sec": timeout_sec,
    }


def ib_client_public_defaults() -> Dict[str, Any]:
    """Fallback ``ib_client`` when status assembly cannot read settings."""
    return {
        "client": {
            "host_ip": "127.0.0.1",
            "host_port_type": "tws_paper",
            "host_port": None,
            "secondary_host_ip": None,
            "secondary_port_type": None,
            "secondary_port": None,
        },
        "port": {
            "trading": 1,
            "listener_host": 2,
            "listener_secondary": 3,
            "operator_host": 100,
            "operator_secondary": 102,
            "ingestor": 150,
            "account_agent": 151,
            "account_agent_secondary": 152,
            "market_data_worker": 500,
        },
        "account": {
            "trading": None,
            "event_host": None,
            "event_secondary": None,
        },
        "timeout_sec": 60.0,
    }


def ib_flex_for_status_api(merged_ib: Dict[str, Any], flex_cfg: Any) -> Dict[str, Any]:
    """``config.ib_flex``: range days from merged settings + token/rows from reader."""
    m = merged_ib or {}
    if isinstance(flex_cfg, dict):
        rows_raw = flex_cfg.get("rows")
        rows: List[Any] = rows_raw if isinstance(rows_raw, list) else []
        host_tok = flex_cfg.get("host_token")
        sec_tok = flex_cfg.get("secondary_token")
    else:
        rows = []
        host_tok = None
        sec_tok = None

    def _days(key: str, default: int) -> int:
        v = m.get(key)
        if v is None:
            return default
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return default

    return {
        "default_range_days": _days("flex_default_range_days", 30),
        "init_range_days": _days("flex_init_range_days", 360),
        "host_token": host_tok,
        "secondary_token": sec_tok,
        "rows": rows,
    }


def ib_flex_public_defaults() -> Dict[str, Any]:
    return {
        "default_range_days": 30,
        "init_range_days": 360,
        "host_token": None,
        "secondary_token": None,
        "rows": [],
    }
