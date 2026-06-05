"""IB Operator health in Redis as a Hash (same field style as bifrost:health:ws_ib_ingestor: string fields, HSET).

Per-slot fields use ``host_*`` (primary TWS / cmd RPC) and ``secondary_*`` (optional second IB host).
Legacy ``operator_*`` / ``account2_*`` names are still read for backward compatibility; new writes use
only the host/secondary names and strip legacy fields from the hash.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

# Written by older versions; removed on each health refresh so HGETALL is not ambiguous.
LEGACY_OPERATOR_HEALTH_HASH_FIELDS: tuple[str, ...] = (
    "operator_connected",
    "operator_client_id",
    "operator_last_error",
    "operator_alive",
    "account2_present",
    "account2_connected",
    "account2_client_id",
    "account2_last_error",
    "account2_reconnects",
    "reconnects",  # was host-slot only before host_reconnects
)


def prune_legacy_operator_health_hash_fields(r: Any, key: str) -> None:
    """HDEL deprecated hash fields after writing canonical host_/secondary_ keys."""
    try:
        r.hdel(key, *LEGACY_OPERATOR_HEALTH_HASH_FIELDS)
    except Exception:
        pass


def _truthy_field(v: Optional[str]) -> bool:
    if v is None:
        return False
    return v.strip() in ("1", "true", "True", "yes", "Yes")


def jsonish_connected(v: Any) -> bool:
    """Normalize ``connected`` from nested JSON/Redis (Python ``bool(\"0\")`` is True — avoid that)."""
    if v is True:
        return True
    if v is False or v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        return s in ("1", "true", "yes")
    return False


def _err_from_field(v: Optional[str]) -> Optional[str]:
    if v is None or v == "":
        return None
    return v


def _safe_int(v: Optional[str], default: int = 0) -> int:
    try:
        return int(v) if v is not None and str(v).strip() != "" else default
    except (TypeError, ValueError):
        return default


def _field_map(m: Dict[str, str], new_key: str, *legacy_keys: str) -> Optional[str]:
    v = m.get(new_key)
    if v is not None and str(v).strip() != "":
        return v
    for k in legacy_keys:
        v2 = m.get(k)
        if v2 is not None and str(v2).strip() != "":
            return v2
    return None


def operator_health_dict_to_redis_hash(h: Dict[str, Any]) -> Dict[str, str]:
    """Flatten ``executor.health_dict()`` (+ ``updated_at``) to Redis hash string fields."""
    host = h.get("host") if isinstance(h.get("host"), dict) else {}
    if not host and isinstance(h.get("operator"), dict):
        host = h["operator"]
    updated = float(h.get("updated_at", time.time()) or time.time())
    last_cmd = float(h.get("last_cmd_ts", 0) or 0)
    cmd_count = int(h.get("cmd_count", 0) or 0)
    # Avoid truthiness bugs (e.g. non-empty string "0" is truthy in Python).
    svc_alive = jsonish_connected(h.get("service_alive", h.get("operator_alive", True)))
    h_probe_at = float(host.get("ib_probe_at") or 0)
    h_probe_iv = float(host.get("ib_probe_interval_sec") or 0)
    sh_iv = float(h.get("service_heartbeat_interval_sec") or 0)
    sh_last = float(h.get("last_service_heartbeat_at") or 0)
    sh_next = float(h.get("next_service_heartbeat_in_s") or 0)
    mapping: Dict[str, str] = {
        "host_connected": "1" if jsonish_connected(host.get("connected")) else "0",
        "host_client_id": str(int(host.get("client_id") or 0)),
        "host_last_error": "" if host.get("last_error") is None else str(host.get("last_error")),
        "host_alive": "1" if svc_alive else "0",
        "host_reconnects": str(int(host.get("reconnects") or 0)),
        "host_ib_probe_at": str(h_probe_at),
        "host_ib_probe_ok": "1" if jsonish_connected(host.get("ib_probe_ok")) else "0",
        "host_ib_probe_interval_sec": str(h_probe_iv),
        "msg_count": str(cmd_count),
        "last_msg_ts": str(last_cmd if last_cmd > 0 else updated),
        "updated_at": str(updated),
        "service_heartbeat_interval_sec": str(sh_iv),
        "last_service_heartbeat_at": str(sh_last),
        "next_service_heartbeat_in_s": str(sh_next),
        "service_heartbeat_reconnect_in_progress": (
            "" if h.get("service_heartbeat_reconnect_in_progress") is None
            else str(h.get("service_heartbeat_reconnect_in_progress"))
        ),
    }
    sec = h.get("secondary")
    if sec is None and h.get("account2") is not None:
        sec = h.get("account2")
    if sec is not None and isinstance(sec, dict):
        s_probe_at = float(sec.get("ib_probe_at") or 0)
        s_probe_iv = float(sec.get("ib_probe_interval_sec") or 0)
        mapping["secondary_present"] = "1"
        mapping["secondary_connected"] = "1" if jsonish_connected(sec.get("connected")) else "0"
        mapping["secondary_client_id"] = str(int(sec.get("client_id") or 0))
        mapping["secondary_last_error"] = (
            "" if sec.get("last_error") is None else str(sec.get("last_error"))
        )
        mapping["secondary_reconnects"] = str(int(sec.get("reconnects") or 0))
        mapping["secondary_ib_probe_at"] = str(s_probe_at)
        mapping["secondary_ib_probe_ok"] = "1" if jsonish_connected(sec.get("ib_probe_ok")) else "0"
        mapping["secondary_ib_probe_interval_sec"] = str(s_probe_iv)
    else:
        # Always overwrite secondary_* so HSET does not leave stale fields from an older run.
        mapping["secondary_present"] = "0"
        mapping["secondary_connected"] = "0"
        mapping["secondary_client_id"] = "0"
        mapping["secondary_last_error"] = ""
        mapping["secondary_reconnects"] = "0"
        mapping["secondary_ib_probe_at"] = "0"
        mapping["secondary_ib_probe_ok"] = "0"
        mapping["secondary_ib_probe_interval_sec"] = "0"
    return mapping


def operator_health_dict_from_redis_hash(m: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Rebuild nested health dict for ``build_monitor_ib_status``; None if ``m`` is empty."""
    if not m:
        return None
    _host_rc = _safe_int(_field_map(m, "host_reconnects", "reconnects"))
    _mc = _safe_int(m.get("msg_count"))
    host_alive_raw = _field_map(m, "host_alive", "operator_alive") or "1"
    def _probe_float(key: str, default: float = 0.0) -> float:
        raw = m.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    _sh_iv = _probe_float("service_heartbeat_interval_sec")
    _sh_last = _probe_float("last_service_heartbeat_at")
    _sh_next = _probe_float("next_service_heartbeat_in_s")
    out: Dict[str, Any] = {
        "host": {
            "connected": _truthy_field(_field_map(m, "host_connected", "operator_connected")),
            "client_id": _safe_int(_field_map(m, "host_client_id", "operator_client_id")),
            "last_error": _err_from_field(_field_map(m, "host_last_error", "operator_last_error")),
            "reconnects": _host_rc,
            "ib_probe_at": _probe_float("host_ib_probe_at"),
            "ib_probe_ok": _truthy_field(m.get("host_ib_probe_ok")),
            "ib_probe_interval_sec": _probe_float("host_ib_probe_interval_sec"),
        },
        "service_alive": _truthy_field(host_alive_raw),
        "msg_count": _mc,
        "service_heartbeat_interval_sec": _sh_iv,
        "last_service_heartbeat_at": _sh_last,
        "next_service_heartbeat_in_s": _sh_next,
        "service_heartbeat_reconnect_in_progress": (
            (m.get("service_heartbeat_reconnect_in_progress") or "").strip()
        ),
    }
    try:
        out["last_msg_ts"] = float(m.get("last_msg_ts") or 0)
    except (TypeError, ValueError):
        out["last_msg_ts"] = 0.0

    sec_on = m.get("secondary_present") == "1" or m.get("account2_present") == "1"
    if sec_on:
        out["secondary"] = {
            "connected": _truthy_field(_field_map(m, "secondary_connected", "account2_connected")),
            "client_id": _safe_int(_field_map(m, "secondary_client_id", "account2_client_id")),
            "last_error": _err_from_field(_field_map(m, "secondary_last_error", "account2_last_error")),
            "reconnects": _safe_int(_field_map(m, "secondary_reconnects", "account2_reconnects")),
            "ib_probe_at": _probe_float("secondary_ib_probe_at"),
            "ib_probe_ok": _truthy_field(m.get("secondary_ib_probe_ok")),
            "ib_probe_interval_sec": _probe_float("secondary_ib_probe_interval_sec"),
        }
    else:
        out["secondary"] = None

    # In-memory / JSON-legacy aliases for callers that still expect operator/account2.
    out["operator"] = out["host"]
    out["operator_alive"] = out["service_alive"]
    out["account2"] = out["secondary"]
    return out


def normalize_operator_health_payload(d: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize legacy string-JSON health (operator/account2) to host/secondary."""
    out = dict(d)
    if "host" not in out and isinstance(out.get("operator"), dict):
        out["host"] = out["operator"]
    if "secondary" not in out and "account2" in out:
        out["secondary"] = out["account2"]
    if "service_alive" not in out and "operator_alive" in out:
        out["service_alive"] = out["operator_alive"]
    return out
