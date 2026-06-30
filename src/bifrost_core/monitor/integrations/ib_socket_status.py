"""Unified Monitor GET /status assembly for IB Broker socket services.

All three services (ib_ingestor, ib_account_agent, ib_operator) expose the same
nested shape: top-level roll-up fields + ``host`` slot + optional ``secondary``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Literal, Optional

from bifrost_core.ib_operator.health_redis import (
    jsonish_connected,
    operator_health_dict_from_redis_hash,
)
from bifrost_core.monitor.integrations.ib_probe_derived import (
    attach_ib_probe_derived,
    attach_service_heartbeat_derived,
    parse_redis_probe_triple,
)

IbBrokerServiceId = Literal["ib_ingestor", "ib_account_agent", "ib_operator"]

IB_HEALTH_FRESH_MAX_S = 180.0

# Canonical Redis probe keys (see bifrost_core.core.redis_health_keys).
INGESTOR_PROBE_KEYS = ("ib_probe_at", "ib_probe_ok", "ib_probe_interval_sec")
HOST_PROBE_KEYS = ("host_ib_probe_at", "host_ib_probe_ok", "host_ib_probe_interval_sec")
SECONDARY_PROBE_KEYS = (
    "secondary_ib_probe_at",
    "secondary_ib_probe_ok",
    "secondary_ib_probe_interval_sec",
)

# Legacy account-agent writer keys (read until all deployments use canonical names).
_HOST_PROBE_LEGACY = ("host_probe_at", "host_probe_ok", "host_probe_interval_sec")
_SECONDARY_PROBE_LEGACY = (
    "secondary_probe_at",
    "secondary_probe_ok",
    "secondary_probe_interval_sec",
)


def _truthy_field(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return default
        return int(v)
    except (TypeError, ValueError):
        return default


def _probe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _field_first(h: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in h and h[k] is not None and str(h[k]).strip() != "":
            return h[k]
    return None


def _parse_probe_triple_with_legacy(
    h: Dict[str, Any],
    canonical: tuple[str, str, str],
    legacy: tuple[str, str, str],
) -> tuple[float, bool, float]:
    at_key = canonical[0] if _field_first(h, canonical[0]) is not None else legacy[0]
    ok_key = canonical[1] if _field_first(h, canonical[1]) is not None else legacy[1]
    iv_key = canonical[2] if _field_first(h, canonical[2]) is not None else legacy[2]
    return parse_redis_probe_triple(h, at_key, ok_key, iv_key)


def _attach_slot_probe(
    slot: Dict[str, Any],
    probe_at: float,
    probe_ok: bool,
    probe_iv: float,
    *,
    now: float,
    stale_mult: float,
) -> None:
    attach_ib_probe_derived(
        slot,
        probe_at=probe_at,
        probe_interval=probe_iv,
        probe_ok=probe_ok,
        stale_mult=stale_mult,
        now=now,
    )


def ib_slot_probe_unhealthy(
    slot: Optional[Dict[str, Any]],
) -> bool:
    """True when IB probe fields indicate staleness or failure."""
    if not slot:
        return False
    if slot.get("ib_probe_stale") is True:
        return True
    pa = slot.get("last_ib_probe_at")
    if isinstance(pa, (int, float)) and pa > 0 and slot.get("ib_probe_ok") is False:
        return True
    return False


def secondary_slot_configured(
    service_id: IbBrokerServiceId,
    ib_cfg: Dict[str, Any],
    redis_hash: Optional[Dict[str, Any]] = None,
) -> bool:
    """Whether Monitor should expose a ``secondary`` slot for this service."""
    ib2_host = str(ib_cfg.get("ib2_host") or "").strip()
    if service_id == "ib_operator":
        try:
            cid2 = int(ib_cfg.get("ib2_client_id_operator") or 102)
        except (TypeError, ValueError):
            cid2 = 102
        return bool(ib2_host) or cid2 != 102
    if service_id == "ib_account_agent":
        try:
            cid2 = int(ib_cfg.get("ib2_client_id_account_agent") or 152)
        except (TypeError, ValueError):
            cid2 = 152
        if ib2_host:
            return True
        if redis_hash is not None:
            if redis_hash.get("secondary_present") == "1":
                return True
            if _field_first(redis_hash, "secondary_client_id") is not None:
                return True
        return cid2 != 152
    return False


def _default_client_id(service_id: IbBrokerServiceId, ib_cfg: Dict[str, Any], slot: str) -> int:
    if service_id == "ib_ingestor":
        return _safe_int(
            ib_cfg.get("client_id_market_gateway")
            or ib_cfg.get("ib_client_id_market_gateway")
            or ib_cfg.get("client_id_ib_ingestor")
            or ib_cfg.get("ib_client_id_ib_ingestor"),
            150,
        )
    if service_id == "ib_account_agent":
        if slot == "secondary":
            return _safe_int(ib_cfg.get("ib2_client_id_account_agent"), 152)
        return _safe_int(ib_cfg.get("client_id_account_agent") or ib_cfg.get("ib_client_id_account_agent"), 151)
    if slot == "secondary":
        return _safe_int(ib_cfg.get("ib2_client_id_operator"), 102)
    return _safe_int(ib_cfg.get("client_id_operator") or ib_cfg.get("ib_client_id_operator"), 100)


def _build_slot(
    *,
    connected: bool,
    client_id: int,
    reconnects: int,
    last_error: Optional[str],
    probe_at: float,
    probe_ok: bool,
    probe_iv: float,
    now: float,
    stale_mult: float,
) -> Dict[str, Any]:
    slot: Dict[str, Any] = {
        "connected": connected,
        "client_id": client_id,
        "last_error": last_error,
        "reconnects": reconnects,
    }
    _attach_slot_probe(slot, probe_at, probe_ok, probe_iv, now=now, stale_mult=stale_mult)
    return slot


def _last_msg_age_s(h: Dict[str, Any], now: float) -> Optional[float]:
    raw = h.get("last_msg_ts")
    if raw is None:
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return max(0.0, now - ts)


def _attach_service_heartbeat(out: Dict[str, Any], h: Dict[str, Any], now: float) -> None:
    iv = _probe_float(h.get("service_heartbeat_interval_sec"))
    last = _probe_float(h.get("last_service_heartbeat_at"))
    if iv > 0:
        attach_service_heartbeat_derived(out, interval_sec=iv, last_heartbeat_at=last, now=now)
    shr = str(h.get("service_heartbeat_reconnect_in_progress") or "").strip()
    out["service_heartbeat_reconnect_in_progress"] = shr if shr else None


def build_ib_socket_status(
    service_id: IbBrokerServiceId,
    redis_hash: Optional[Dict[str, Any]],
    ib_cfg: Dict[str, Any],
    *,
    now: Optional[float] = None,
    stale_mult: float = 2.5,
    unreachable: str = "IB service unreachable (process not writing Redis health)",
) -> Dict[str, Any]:
    """Build unified ``socket.{service_id}`` block from a flat Redis health hash."""
    ts = float(now if now is not None else time.time())
    cfg = ib_cfg or {}

    if not redis_hash:
        host_cid = _default_client_id(service_id, cfg, "host")
        out: Dict[str, Any] = {
            "connected": False,
            "service_alive": False,
            "operator_alive": False,
            "last_msg_age_s": None,
            "reconnects": 0,
            "msg_count": 0,
            "host": {
                "connected": False,
                "client_id": host_cid,
                "last_error": unreachable,
                "reconnects": 0,
            },
            "secondary": None,
            "service_heartbeat_reconnect_in_progress": None,
        }
        if secondary_slot_configured(service_id, cfg):
            sec_cid = _default_client_id(service_id, cfg, "secondary")
            out["secondary"] = {
                "connected": False,
                "client_id": sec_cid,
                "last_error": unreachable,
                "reconnects": 0,
            }
        if service_id == "ib_ingestor":
            out["client_id"] = host_cid
        else:
            out["client_id"] = host_cid
        return out

    h = redis_hash

    if service_id == "ib_ingestor":
        host_on = _truthy_field(_field_first(h, "connected", "host_connected"))
        host_cid = _safe_int(_field_first(h, "client_id", "host_client_id"), _default_client_id(service_id, cfg, "host"))
        host_rc = _safe_int(h.get("reconnects") or h.get("host_reconnects"))
        pa, pok, piv = _parse_probe_triple_with_legacy(h, INGESTOR_PROBE_KEYS, HOST_PROBE_KEYS)
        if pa <= 0:
            pa, pok, piv = parse_redis_probe_triple(h, *HOST_PROBE_KEYS)
        host = _build_slot(
            connected=host_on,
            client_id=host_cid,
            reconnects=host_rc,
            last_error=None,
            probe_at=pa,
            probe_ok=pok,
            probe_iv=piv,
            now=ts,
            stale_mult=stale_mult,
        )
        out = {
            "connected": host_on,
            "service_alive": True,
            "last_msg_age_s": _last_msg_age_s(h, ts),
            "reconnects": host_rc,
            "msg_count": _safe_int(h.get("msg_count")),
            "client_id": host_cid,
            "host": host,
            "secondary": None,
        }
        # Back-compat: mirror probe fields at top level for legacy consumers.
        for k in (
            "last_ib_probe_at",
            "ib_probe_interval_sec",
            "ib_probe_ok",
            "next_ib_probe_in_s",
            "ib_probe_stale",
        ):
            if k in host:
                out[k] = host[k]
        _attach_service_heartbeat(out, h, ts)
        return out

    if service_id == "ib_account_agent":
        host_on = _truthy_field(_field_first(h, "host_connected", "connected"))
        host_cid = _safe_int(
            _field_first(h, "host_client_id", "client_id"),
            _default_client_id(service_id, cfg, "host"),
        )
        host_rc = _safe_int(_field_first(h, "host_reconnects", "reconnects"))
        host_err_raw = _field_first(h, "host_last_error")
        host_err = None if host_err_raw in (None, "") else str(host_err_raw)
        hpa, hpok, hpiv = _parse_probe_triple_with_legacy(h, HOST_PROBE_KEYS, _HOST_PROBE_LEGACY)
        host = _build_slot(
            connected=host_on,
            client_id=host_cid,
            reconnects=host_rc,
            last_error=host_err,
            probe_at=hpa,
            probe_ok=hpok,
            probe_iv=hpiv,
            now=ts,
            stale_mult=stale_mult,
        )
        if "host_alive" in h:
            alive = _truthy_field(h.get("host_alive"))
        else:
            alive = True
        out: Dict[str, Any] = {
            "connected": host_on,
            "service_alive": alive,
            "operator_alive": alive,
            "last_msg_age_s": _last_msg_age_s(h, ts),
            "reconnects": host_rc,
            "msg_count": _safe_int(h.get("msg_count")),
            "client_id": host_cid,
            "host": host,
            "secondary": None,
        }
        if secondary_slot_configured(service_id, cfg, h):
            sec_on = _truthy_field(h.get("secondary_connected"))
            sec_cid = _safe_int(h.get("secondary_client_id"), _default_client_id(service_id, cfg, "secondary"))
            sec_rc = _safe_int(_field_first(h, "secondary_reconnects", "reconnects"))
            sec_err_raw = _field_first(h, "secondary_last_error")
            sec_err = None if sec_err_raw in (None, "") else str(sec_err_raw)
            spa, spok, spiv = _parse_probe_triple_with_legacy(h, SECONDARY_PROBE_KEYS, _SECONDARY_PROBE_LEGACY)
            out["secondary"] = _build_slot(
                connected=sec_on,
                client_id=sec_cid,
                reconnects=sec_rc,
                last_error=sec_err,
                probe_at=spa,
                probe_ok=spok,
                probe_iv=spiv,
                now=ts,
                stale_mult=stale_mult,
            )
        _attach_service_heartbeat(out, h, ts)
        return out

    # ib_operator — flat hash (ingest-style) or nested via operator_health_dict_from_redis_hash.
    nested = operator_health_dict_from_redis_hash({str(k): str(v) for k, v in h.items()})
    if nested is None:
        return build_ib_socket_status(service_id, None, cfg, now=ts, stale_mult=stale_mult, unreachable=unreachable)

    host_h = nested.get("host") if isinstance(nested.get("host"), dict) else {}
    host_cid = _safe_int(host_h.get("client_id"), _default_client_id(service_id, cfg, "host"))
    host = _build_slot(
        connected=jsonish_connected(host_h.get("connected")),
        client_id=host_cid,
        reconnects=_safe_int(host_h.get("reconnects")),
        last_error=host_h.get("last_error"),
        probe_at=_probe_float(host_h.get("ib_probe_at")),
        probe_ok=_truthy_field(host_h.get("ib_probe_ok")),
        probe_iv=_probe_float(host_h.get("ib_probe_interval_sec")),
        now=ts,
        stale_mult=stale_mult,
    )
    alive = jsonish_connected(nested.get("service_alive", True))
    out = {
        "connected": jsonish_connected(host.get("connected")),
        "service_alive": alive,
        "operator_alive": alive,
        "last_msg_age_s": _last_msg_age_s(nested, ts),
        "reconnects": _safe_int(host.get("reconnects")),
        "msg_count": _safe_int(nested.get("msg_count")),
        "client_id": host_cid,
        "host": host,
        "secondary": None,
    }
    if secondary_slot_configured(service_id, cfg, h):
        sec_h = nested.get("secondary")
        if isinstance(sec_h, dict):
            sec_cid = _safe_int(sec_h.get("client_id"), _default_client_id(service_id, cfg, "secondary"))
            out["secondary"] = _build_slot(
                connected=jsonish_connected(sec_h.get("connected")),
                client_id=sec_cid,
                reconnects=_safe_int(sec_h.get("reconnects")),
                last_error=sec_h.get("last_error"),
                probe_at=_probe_float(sec_h.get("ib_probe_at")),
                probe_ok=_truthy_field(sec_h.get("ib_probe_ok")),
                probe_iv=_probe_float(sec_h.get("ib_probe_interval_sec")),
                now=ts,
                stale_mult=stale_mult,
            )
        else:
            sec_cid = _default_client_id(service_id, cfg, "secondary")
            ib2_host = str(cfg.get("ib2_host") or "").strip()
            out["secondary"] = {
                "connected": False,
                "client_id": sec_cid,
                "last_error": (
                    unreachable
                    if not h
                    else ("Set Second IB host in Settings to enable" if not ib2_host else None)
                ),
                "reconnects": 0,
            }
    _attach_service_heartbeat(out, nested, ts)
    return out


def rollup_ib_broker_lamp(status_block: Dict[str, Any]) -> Dict[str, str]:
    """Single roll-up rules for all IB Broker socket services."""
    host = status_block.get("host") if isinstance(status_block.get("host"), dict) else {}
    secondary = status_block.get("secondary")
    sec = secondary if isinstance(secondary, dict) else None
    sec_configured = sec is not None

    proc_dead = status_block.get("service_alive") is False or status_block.get("operator_alive") is False
    host_up = jsonish_connected(status_block.get("connected")) or jsonish_connected(host.get("connected"))
    host_up = host_up and not proc_dead

    last_age = status_block.get("last_msg_age_s")
    health_fresh = (
        isinstance(last_age, (int, float))
        and float(last_age) <= IB_HEALTH_FRESH_MAX_S
    )

    if ib_slot_probe_unhealthy(host):
        return {
            "lamp": "red",
            "title": "IB Host probe stale or failed (Redis health hash).",
        }
    if host_up:
        if sec_configured and ib_slot_probe_unhealthy(sec):
            return {
                "lamp": "yellow",
                "title": "IB Host connected; Secondary probe stale or failed.",
            }
        if sec_configured and not jsonish_connected(sec.get("connected")):
            return {
                "lamp": "yellow",
                "title": "IB Host connected; Secondary not connected.",
            }
        return {"lamp": "green", "title": "IB Broker service healthy (Host + Secondary if configured)."}
    if proc_dead:
        return {"lamp": "red", "title": "IB service process reports stopped (Redis service_alive)."}
    service_alive = status_block.get("service_alive")
    if service_alive is not False and not host_up and health_fresh:
        return {
            "lamp": "yellow",
            "title": "IB service running; Host not connected yet.",
        }
    if service_alive is not False and not host_up and not health_fresh:
        return {
            "lamp": "red",
            "title": "IB Host not connected; Redis health stale or missing timestamp.",
        }
    return {"lamp": "red", "title": "IB Host not connected (Redis health hash)."}
