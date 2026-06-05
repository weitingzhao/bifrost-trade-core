"""Canonical Redis keys for Socket ingest health under ``bifrost:health:*``.

Service **ids** in Ops YAML stay ``massive_ws`` / ``ib_ingestor`` / ``ib_operator``; Redis **health**
hashes use the ``ws_*`` suffix names below. Strategy Trading Daemon health + Ops lease use
``bifrost:health:daemon_strategy_trading``.

Readers fall back to prior bifrost key names and (Massive only) ``massive:meta:status`` when the
canonical hash is empty.
"""

from __future__ import annotations

from typing import Any, Dict

# Health hash TTL: each service heartbeat (30 s) resets this; expiry means process is dead.
# 6× heartbeat interval → safe margin for transient pauses.
HEALTH_HASH_TTL_SEC = 180  # 3 minutes

# Deprecated Ops control-lease keys. Socket Services now store Dev/Prod HOST fields directly
# on their bifrost:health:* hashes because Prod Redis writes those nodes reliably.
BIFROST_OPS_LEASE_PREFIX = "bifrost:ops:lease:"

BIFROST_OPS_LEASE_MASSIVE_WS = BIFROST_OPS_LEASE_PREFIX + "massive_ws"
BIFROST_OPS_LEASE_IB_INGESTOR = BIFROST_OPS_LEASE_PREFIX + "ib_ingestor"
BIFROST_OPS_LEASE_IB_OPERATOR = BIFROST_OPS_LEASE_PREFIX + "ib_operator"
BIFROST_OPS_LEASE_IB_ACCOUNT_AGENT = BIFROST_OPS_LEASE_PREFIX + "ib_account_agent"


def ops_lease_key_for_service(service_id: str) -> str:
    """Return the legacy Ops control-lease Redis key for a service_id."""
    return BIFROST_OPS_LEASE_PREFIX + service_id.strip()


# Canonical health hashes (Socket Services / GET /status ``socket`` + Ops ``redis_meta_key``).
# IB hashes may include per-slot ``*_ib_probe_at``, ``*_ib_probe_ok``, ``*_ib_probe_interval_sec``
# (Operator host/secondary; Account Agent host/secondary; Ingestor ``ib_probe_*``) for liveness UI.
BIFROST_HEALTH_MASSIVE_WS = "bifrost:health:ws_massive_option"
BIFROST_HEALTH_IB_INGESTOR = "bifrost:health:ws_ib_ingestor"
BIFROST_HEALTH_IB_OPERATOR = "bifrost:health:ws_ib_operator"
BIFROST_HEALTH_IB_ACCOUNT_AGENT = "bifrost:health:ws_ib_account_agent"

# Account Sync Daemon: independent process that consumes ib:account:stream:v1 and
# persists Account / Position / Execution data to PostgreSQL.
BIFROST_HEALTH_ACCOUNT_SYNC_DAEMON = "bifrost:health:daemon_account_sync"
LEGACY_BIFROST_HEALTH_ACCOUNT_SYNC_DAEMON = "bifrost:health:account_sync_daemon"

# Strategy Trading Daemon: health hash + Ops Dev/Prod lease fields (``bifrost_ops_control_*``,
# ``engine_ops_active``) on the same key — NOT migrated to separate lease key (different lifecycle).
BIFROST_HEALTH_DAEMON_TRADING_ENGINE = "bifrost:health:daemon_strategy_trading"
LEGACY_BIFROST_HEALTH_DAEMON_TRADING_ENGINE = "bifrost:health:daemon_trading_engine"
# Previous key (YAML / Redis migration); normalized in ``market_ingest_config``.
LEGACY_BIFROST_OPS_TRADING_ENGINE_META = "bifrost:ops:trading_engine"
# Deprecated alias — prefer ``BIFROST_HEALTH_DAEMON_TRADING_ENGINE``.
BIFROST_OPS_TRADING_ENGINE_META = BIFROST_HEALTH_DAEMON_TRADING_ENGINE
ENGINE_OPS_ACTIVE_REDIS_FIELD = "engine_ops_active"

# Previous bifrost names (read / YAML normalization fallback).
LEGACY_BIFROST_MASSIVE_WS = "bifrost:health:massive_ws"
LEGACY_BIFROST_IB_INGESTOR = "bifrost:health:ib_ingestor"
LEGACY_BIFROST_IB_OPERATOR = "bifrost:health:ib_operator"
LEGACY_BIFROST_IB_ACCOUNT_AGENT = "bifrost:health:ib_account_agent"

# Older Massive key (read fallback).
LEGACY_MASSIVE_META_STATUS = "massive:meta:status"

# Deprecated IB operator key — YAML normalization only.
LEGACY_IB_OPERATOR_META_HEALTH = "ib:operator:meta:health"


def redis_hash_field_truthy(h: Dict[str, Any], field: str = "connected") -> bool:
    """Coerce a Redis hash field to bool (writers use ``\"1\"`` / ``\"0\"``; tolerate int/bool/whitespace)."""
    if not h:
        return False
    v = h.get(field)
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")


def hgetall_massive_ws_status(r: Any) -> Dict[str, str]:
    """Massive WS / options ingest health hash (same field names as before)."""
    for key in (
        BIFROST_HEALTH_MASSIVE_WS,
        LEGACY_BIFROST_MASSIVE_WS,
        LEGACY_MASSIVE_META_STATUS,
    ):
        h = r.hgetall(key)
        if h:
            return dict(h)
    return {}


def hgetall_ib_ingestor_health(r: Any) -> Dict[str, str]:
    """IB market ingestor health hash."""
    h = r.hgetall(BIFROST_HEALTH_IB_INGESTOR)
    if not h:
        h = r.hgetall(LEGACY_BIFROST_IB_INGESTOR)
    return dict(h or {})


def hgetall_ib_account_agent_health(r: Any) -> Dict[str, str]:
    """IB Account Agent health hash (account-domain events → Redis only)."""
    h = r.hgetall(BIFROST_HEALTH_IB_ACCOUNT_AGENT)
    if not h:
        h = r.hgetall(LEGACY_BIFROST_IB_ACCOUNT_AGENT)
    return dict(h or {})


def hgetall_account_sync_daemon_health(r: Any) -> Dict[str, str]:
    """Account Sync Daemon health hash (canonical key, then legacy migration key)."""
    for key in (BIFROST_HEALTH_ACCOUNT_SYNC_DAEMON, LEGACY_BIFROST_HEALTH_ACCOUNT_SYNC_DAEMON):
        h = r.hgetall(key)
        if h:
            return dict(h)
    return {}
