"""Platform IB Gateway health derivation — Trade reads redis-ib, not socket STS.

``bifrost-platform-plugin`` writes legacy-compatible ``bifrost:health:ws_ib_*`` hashes
with ``plugin=ib-gateway`` and ``mode=live|mock``. Monitor + daemon roll up from these keys.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, Tuple

from bifrost_core.core.redis_health_keys import (
    hgetall_ib_account_agent_health,
    hgetall_ib_ingestor_health,
    hgetall_ib_operator_health,
)
from bifrost_core.monitor.integrations.ib_socket_status import (
    IbBrokerServiceId,
    build_ib_socket_status,
    rollup_ib_broker_lamp,
)

IbTransport = Literal["platform_gateway", "legacy_socket"]

PLATFORM_GATEWAY_PLUGIN = "ib-gateway"
PLATFORM_GATEWAY_DEPLOYMENT = "data/ib-gateway"
PLATFORM_GATEWAY_BUS = "redis-ib"
PLATFORM_GATEWAY_UNREACHABLE = (
    "Platform IB Gateway unreachable @ redis-ib — check data/ib-gateway Deployment"
)

_DAEMON_IB_SERVICES: Tuple[IbBrokerServiceId, ...] = (
    "ib_operator",
    "ib_ingestor",
    "ib_account_agent",
)


def is_platform_ib_gateway_health(h: Optional[Dict[str, Any]]) -> bool:
    if not h:
        return False
    if str(h.get("plugin") or "").strip() == PLATFORM_GATEWAY_PLUGIN:
        return True
    mode = str(h.get("mode") or "").strip().lower()
    return mode in ("live", "mock")


def detect_ib_transport(*hashes: Optional[Dict[str, Any]]) -> IbTransport:
    if any(is_platform_ib_gateway_health(h) for h in hashes):
        return "platform_gateway"
    return "legacy_socket"


def _gateway_mode(*hashes: Optional[Dict[str, Any]]) -> Optional[str]:
    for h in hashes:
        if not h:
            continue
        mode = str(h.get("mode") or "").strip().lower()
        if mode in ("live", "mock"):
            return mode
    return None


def annotate_ib_socket_transport(
    block: Optional[Dict[str, Any]],
    transport: IbTransport,
) -> Optional[Dict[str, Any]]:
    if block is None or not isinstance(block, dict):
        return block
    out = dict(block)
    out["transport"] = transport
    out["health_source"] = (
        "platform_ib_gateway" if transport == "platform_gateway" else "legacy_socket"
    )
    return out


def _read_health_hash(r: Any, service_id: IbBrokerServiceId) -> Dict[str, str]:
    if service_id == "ib_ingestor":
        return hgetall_ib_ingestor_health(r)
    if service_id == "ib_account_agent":
        return hgetall_ib_account_agent_health(r)
    return hgetall_ib_operator_health(r)


def _unreachable_for_transport(transport: IbTransport, service_id: IbBrokerServiceId) -> str:
    if transport == "platform_gateway":
        return PLATFORM_GATEWAY_UNREACHABLE
    if service_id == "ib_account_agent":
        return (
            "IB Account Agent unreachable "
            "(Platform IB Gateway @ redis-ib — check data/ib-gateway)"
        )
    return f"IB {service_id} unreachable (process not writing Redis health)"


def build_ib_socket_blocks_from_redis(
    r: Any,
    ib_cfg: Dict[str, Any],
    *,
    now: float,
    stale_mult: float,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], IbTransport]:
    raw: Dict[IbBrokerServiceId, Dict[str, str]] = {}
    blocks: Dict[IbBrokerServiceId, Dict[str, Any]] = {}
    for sid in _DAEMON_IB_SERVICES:
        raw[sid] = _read_health_hash(r, sid) or {}
    transport = detect_ib_transport(raw["ib_ingestor"], raw["ib_account_agent"], raw["ib_operator"])
    for sid in _DAEMON_IB_SERVICES:
        blocks[sid] = build_ib_socket_status(
            sid,
            raw[sid] or None,
            ib_cfg,
            now=now,
            stale_mult=stale_mult,
            unreachable=_unreachable_for_transport(transport, sid),
        )
        blocks[sid] = annotate_ib_socket_transport(blocks[sid], transport) or blocks[sid]
    return blocks["ib_ingestor"], blocks["ib_account_agent"], blocks["ib_operator"], transport


def rollup_platform_ib_gateway_lamp(
    ingestor: Dict[str, Any],
    account_agent: Dict[str, Any],
    operator: Dict[str, Any],
) -> Dict[str, str]:
    lamps = [
        rollup_ib_broker_lamp(ingestor).get("lamp") or "red",
        rollup_ib_broker_lamp(account_agent).get("lamp") or "red",
        rollup_ib_broker_lamp(operator).get("lamp") or "red",
    ]
    if all(lamp == "green" for lamp in lamps):
        return {
            "lamp": "green",
            "title": "Platform IB Gateway healthy (ingestor + account + operator via redis-ib).",
        }
    if any(lamp == "red" for lamp in lamps):
        return {
            "lamp": "red",
            "title": "Platform IB Gateway degraded — one or more redis-ib health components red.",
        }
    return {
        "lamp": "yellow",
        "title": "Platform IB Gateway partial — check Host/Secondary TWS slots on data/ib-gateway.",
    }


def build_platform_ib_gateway_status(
    ingestor: Dict[str, Any],
    account_agent: Dict[str, Any],
    operator: Dict[str, Any],
    *,
    transport: IbTransport,
    mode: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if transport != "platform_gateway":
        return None
    rollup = rollup_platform_ib_gateway_lamp(ingestor, account_agent, operator)
    host_op = operator.get("host") if isinstance(operator.get("host"), dict) else {}
    client_id = host_op.get("client_id")
    connected = rollup.get("lamp") == "green"
    return {
        "transport": "platform_gateway",
        "deployment": PLATFORM_GATEWAY_DEPLOYMENT,
        "bus": PLATFORM_GATEWAY_BUS,
        "mode": mode,
        "connected": connected,
        "lamp": rollup.get("lamp"),
        "title": rollup.get("title"),
        "client_id": client_id if isinstance(client_id, int) and client_id > 0 else None,
        "components": {
            "ingestor": ingestor,
            "account_agent": account_agent,
            "operator": operator,
        },
    }


def derive_daemon_ib_heartbeat_from_redis(
    r: Any,
    ib_cfg: Dict[str, Any],
    *,
    now: Optional[float] = None,
    stale_mult: Optional[float] = None,
) -> Dict[str, Any]:
    """Return daemon heartbeat IB fields from redis-ib health (Platform gateway or legacy socket)."""
    import time

    if r is None:
        return {"ib_connected": False, "ib_client_id": None, "ib_transport": "legacy_socket"}

    ts = float(now if now is not None else time.time())
    sm = float(stale_mult if stale_mult is not None else ib_cfg.get("ib_probe_stale_multiplier") or 2.5)

    ingestor, account_agent, operator, transport = build_ib_socket_blocks_from_redis(
        r, ib_cfg, now=ts, stale_mult=sm
    )

    lamps = [
        rollup_ib_broker_lamp(ingestor).get("lamp") or "red",
        rollup_ib_broker_lamp(account_agent).get("lamp") or "red",
        rollup_ib_broker_lamp(operator).get("lamp") or "red",
    ]
    connected = len(lamps) == 3 and all(lamp == "green" for lamp in lamps)

    client_id: Optional[int] = None
    if connected:
        host = operator.get("host") if isinstance(operator.get("host"), dict) else {}
        cid = host.get("client_id")
        if isinstance(cid, int) and cid > 0:
            client_id = cid

    out: Dict[str, Any] = {
        "ib_connected": connected,
        "ib_client_id": client_id,
        "ib_transport": transport,
    }
    mode = _gateway_mode(
        _read_health_hash(r, "ib_ingestor"),
        _read_health_hash(r, "ib_account_agent"),
        _read_health_hash(r, "ib_operator"),
    )
    pg = build_platform_ib_gateway_status(
        ingestor, account_agent, operator, transport=transport, mode=mode
    )
    if pg is not None:
        out["platform_ib_gateway"] = pg
    return out
