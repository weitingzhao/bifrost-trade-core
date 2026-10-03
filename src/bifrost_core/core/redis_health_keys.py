"""Canonical Redis keys for service health under ``bifrost:health:*``.

The IB hashes (``ws_*`` names) were written by the retired Socket services and are now
written by the IB Gateway plugin; service **ids** in Ops YAML are ``ib_ingestor`` /
``ib_operator`` / ``ib_account_agent``. The trading daemon (Deployment ``daemon``) writes
``bifrost:health:daemon_strategy_trading``.

**String values are live Redis keys and are never renamed** — only the Python names are
(TD-75). Readers fall back to prior key names when the canonical hash is empty.
"""

from __future__ import annotations

from typing import Any, Dict

# Health hash TTL: each service heartbeat (30 s) resets this; expiry means process is dead.
# 6× heartbeat interval → safe margin for transient pauses.
HEALTH_HASH_TTL_SEC = 180  # 3 minutes

# Deprecated Ops control-lease keys. Socket Services now store Dev/Prod HOST fields directly
# on their bifrost:health:* hashes because Prod Redis writes those nodes reliably.
BIFROST_OPS_LEASE_PREFIX = "bifrost:ops:lease:"

BIFROST_OPS_LEASE_IB_INGESTOR = BIFROST_OPS_LEASE_PREFIX + "ib_ingestor"
BIFROST_OPS_LEASE_IB_OPERATOR = BIFROST_OPS_LEASE_PREFIX + "ib_operator"
BIFROST_OPS_LEASE_IB_ACCOUNT_AGENT = BIFROST_OPS_LEASE_PREFIX + "ib_account_agent"


def ops_lease_key_for_service(service_id: str) -> str:
    """Return the legacy Ops control-lease Redis key for a service_id."""
    return BIFROST_OPS_LEASE_PREFIX + service_id.strip()


# Canonical health hashes (Socket Services / GET /status ``socket`` + Ops ``redis_meta_key``).
# IB hashes may include per-slot ``*_ib_probe_at``, ``*_ib_probe_ok``, ``*_ib_probe_interval_sec``
# (Operator host/secondary; Account Agent host/secondary; Ingestor ``ib_probe_*``) for liveness UI.
BIFROST_HEALTH_IB_INGESTOR = "bifrost:health:ws_ib_ingestor"
BIFROST_HEALTH_IB_OPERATOR = "bifrost:health:ws_ib_operator"
BIFROST_HEALTH_IB_ACCOUNT_AGENT = "bifrost:health:ws_ib_account_agent"

# Account Sync Daemon: independent process that consumes ib:account:stream:v1 and
# persists Account / Position / Execution data to PostgreSQL.

# Strategy Trading Daemon (Deployment ``daemon``, class ``GsTrading``): health hash + Ops
# Dev/Prod lease fields (``bifrost_ops_control_*``, ``engine_ops_active``) on the same key —
# NOT migrated to a separate lease key (different lifecycle).
# The value is a live Redis key (redis-ib ACL allows ``~bifrost:health:daemon_*``): do not change it.
BIFROST_HEALTH_DAEMON_STRATEGY_TRADING = "bifrost:health:daemon_strategy_trading"
# Deprecated alias (0.39.0, TD-75): the old name said trading_engine while the value says
# strategy_trading. Kept for one core version so api / worker can switch; then removed.
BIFROST_HEALTH_DAEMON_TRADING_ENGINE = BIFROST_HEALTH_DAEMON_STRATEGY_TRADING
# Earlier key names, only normalized away in api ``market_ingest_config`` (Ops YAML meta_key).
# Neither key exists in redis-dev / redis-live-stg / redis-live-prod (checked 2026-10-03).
LEGACY_BIFROST_HEALTH_DAEMON_TRADING_ENGINE = "bifrost:health:daemon_trading_engine"
LEGACY_BIFROST_OPS_TRADING_ENGINE_META = "bifrost:ops:trading_engine"
ENGINE_OPS_ACTIVE_REDIS_FIELD = "engine_ops_active"

# Previous bifrost names (read / YAML normalization fallback).
LEGACY_BIFROST_IB_INGESTOR = "bifrost:health:ib_ingestor"
LEGACY_BIFROST_IB_OPERATOR = "bifrost:health:ib_operator"
LEGACY_BIFROST_IB_ACCOUNT_AGENT = "bifrost:health:ib_account_agent"

# Older IB operator meta health key (read / YAML normalization fallback).
LEGACY_IB_OPERATOR_META_HEALTH = "ib:operator:meta:health"
LEGACY_IB_INGESTER_META_HEALTH = "ib:ingester:meta:health"


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


def hgetall_ib_operator_health(r: Any) -> Dict[str, str]:
    """IB Operator health hash (cmd RPC + optional secondary slot)."""
    h = r.hgetall(BIFROST_HEALTH_IB_OPERATOR)
    if not h:
        h = r.hgetall(LEGACY_BIFROST_IB_OPERATOR)
    return dict(h or {})

