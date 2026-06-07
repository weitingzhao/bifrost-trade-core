"""Derive Strategy Trading Daemon ``ib_connected`` from Socket IB Redis health.

The daemon does not hold an in-process IB socket; ``ib_connected`` mirrors the Daemon
page IB broker group roll-up (Operator + Ingestor + Account Agent all green).
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

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

_DAEMON_IB_SERVICES: Tuple[IbBrokerServiceId, ...] = (
    "ib_operator",
    "ib_ingestor",
    "ib_account_agent",
)


def _read_health_hash(r: Any, service_id: IbBrokerServiceId) -> Dict[str, str]:
    if service_id == "ib_ingestor":
        return hgetall_ib_ingestor_health(r)
    if service_id == "ib_account_agent":
        return hgetall_ib_account_agent_health(r)
    return hgetall_ib_operator_health(r)


def derive_daemon_ib_heartbeat_from_redis(
    r: Any,
    ib_cfg: Dict[str, Any],
    *,
    now: Optional[float] = None,
    stale_mult: Optional[float] = None,
) -> Dict[str, Any]:
    """Return ``ib_connected`` / ``ib_client_id`` for ``daemon_heartbeat`` writes.

    ``ib_connected`` is True only when all three Socket IB services roll up to green
    (same rule as the Daemon page IB broker group lamp).
    """
    if r is None:
        return {"ib_connected": False, "ib_client_id": None}

    ts = float(now if now is not None else time.time())
    if stale_mult is None:
        stale_mult = float(ib_cfg.get("ib_probe_stale_multiplier") or 2.5)

    lamps: list[str] = []
    operator_block: Optional[Dict[str, Any]] = None

    for sid in _DAEMON_IB_SERVICES:
        raw = _read_health_hash(r, sid)
        block = build_ib_socket_status(
            sid,
            raw or None,
            ib_cfg,
            now=ts,
            stale_mult=float(stale_mult),
        )
        if sid == "ib_operator":
            operator_block = block
        lamps.append(rollup_ib_broker_lamp(block).get("lamp") or "red")

    connected = len(lamps) == len(_DAEMON_IB_SERVICES) and all(l == "green" for l in lamps)
    client_id: Optional[int] = None
    if connected and operator_block:
        host = operator_block.get("host") if isinstance(operator_block.get("host"), dict) else {}
        cid = host.get("client_id")
        if isinstance(cid, int) and cid > 0:
            client_id = cid

    return {"ib_connected": connected, "ib_client_id": client_id}
