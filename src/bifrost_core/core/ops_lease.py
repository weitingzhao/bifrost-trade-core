"""Self-healing Ops control lease for Socket Services health heartbeats.

Each service calls ``maintain_health_host`` on every service-heartbeat tick.
If the HOST field is missing from the service health hash (e.g. after a Redis
restart or key eviction), it is re-written automatically without waiting for an
Ops operator to click Start again.

``maintain_service_ops_lease`` is kept for backward-compat but is deprecated;
all Socket Services now write directly to their ``bifrost:health:*`` hash.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_BIFROST_OPS_CONTROL_ENV_FIELD = "bifrost_ops_control_env"
_BIFROST_OPS_CONTROL_HOST_FIELD = "bifrost_ops_control_host"
_BIFROST_OPS_CONTROL_UPDATED_AT_FIELD = "bifrost_ops_control_updated_at"


def ops_profile_from_config(config: dict) -> Optional[str]:
    """Return ``'dev'`` or ``'prod'`` for this service process.

    Priority:
    1. ``ops.control_profile`` key in the loaded YAML config.
    2. ``BIFROST_OPS_CONTROL_PROFILE`` environment variable.
    3. ``BIFROST_ENV`` environment variable (the variable that selects which
       config file is loaded, so it is strongly correlated with the profile).
    """
    ops = config.get("ops") if isinstance(config.get("ops"), dict) else {}
    raw = ops.get("control_profile") if isinstance(ops, dict) else None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("dev", "prod"):
            return s
    for env_key in ("BIFROST_OPS_CONTROL_PROFILE", "BIFROST_ENV"):
        val = os.environ.get(env_key, "").strip().lower()
        if val in ("dev", "prod"):
            return val
    return None


def _current_hostname() -> str:
    try:
        return (socket.gethostname() or "unknown").strip() or "unknown"
    except Exception:
        return "unknown"


def maintain_health_host(r: Any, health_hash_key: str, profile: Optional[str]) -> None:
    """Ensure ``bifrost_ops_control_host`` is present in the service health hash.

    Called on every 30-second service-heartbeat tick so the HOST column in the
    Socket Services page self-heals after a Redis restart or key eviction.

    - HOST present  → refresh ``bifrost_ops_control_updated_at`` (no profile needed).
    - HOST absent + profile set → restore env + host + updated_at (full recovery).
    - HOST absent + profile None → no-op (cannot restore without knowing the env).
    """
    if not health_hash_key:
        return
    try:
        existing_host = r.hget(health_hash_key, _BIFROST_OPS_CONTROL_HOST_FIELD)
        now = time.time()
        if existing_host and str(existing_host).strip():
            # Refresh the countdown timestamp regardless of profile — the entry was
            # written by Ops START so we just need to keep updated_at current.
            r.hset(health_hash_key, _BIFROST_OPS_CONTROL_UPDATED_AT_FIELD, str(now))
        elif profile:
            # Host missing (Redis restart / key eviction) — restore all three fields.
            hostname = _current_hostname()
            r.hset(health_hash_key, mapping={
                _BIFROST_OPS_CONTROL_ENV_FIELD: profile,
                _BIFROST_OPS_CONTROL_HOST_FIELD: hostname,
                _BIFROST_OPS_CONTROL_UPDATED_AT_FIELD: str(now),
            })
            logger.info(
                "ops_lease: restored bifrost_ops_control_host on %s → %s @ %s",
                health_hash_key, profile, hostname,
            )
    except Exception as e:
        logger.debug("maintain_health_host %s: %s", health_hash_key, e)


def maintain_service_ops_lease(r: Any, service_id: str, profile: Optional[str]) -> None:
    """Deprecated: writes to the old ``bifrost:ops:lease:*`` key instead of the health hash.

    Use ``maintain_health_host`` with the service's canonical ``bifrost:health:*`` key instead.
    Kept for any callers not yet migrated.
    """
    if not profile:
        return
    from bifrost_core.core.redis_health_keys import ops_lease_key_for_service
    key = ops_lease_key_for_service(service_id)
    try:
        existing = r.hget(key, _BIFROST_OPS_CONTROL_ENV_FIELD)
        if not existing:
            hostname = _current_hostname()
            r.hset(key, mapping={
                _BIFROST_OPS_CONTROL_ENV_FIELD: profile,
                _BIFROST_OPS_CONTROL_HOST_FIELD: hostname,
            })
            logger.info(
                "ops_lease: restored bifrost:ops:lease:%s → %s @ %s",
                service_id, profile, hostname,
            )
    except Exception as e:
        logger.debug("maintain_service_ops_lease %s: %s", service_id, e)
