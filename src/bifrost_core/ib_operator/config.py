"""Merge ``ib_operator:`` from YAML with defaults (legacy ``ib_gateway`` fallback)."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from bifrost_core.core.redis_health_keys import (
    BIFROST_HEALTH_IB_OPERATOR,
    LEGACY_BIFROST_IB_OPERATOR,
    LEGACY_IB_OPERATOR_META_HEALTH,
)
from bifrost_core.core.redis_url import (
    effective_redis_dict,
    format_redis_url,
    ib_redis_url_from_config,
    redis_url_from_config,
)

logger = logging.getLogger(__name__)


def effective_ib_operator_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return resolved IB Operator settings (stream, group, TTLs, keys).

    ``enabled`` defaults to True when Redis URL is available and ``ib_operator.enabled``
    is not explicitly False.

    If ``ib_operator`` is absent but ``ib_gateway`` is present, reads ``ib_gateway`` and logs a
    deprecation warning.
    """
    raw = config.get("ib_operator")
    if raw is None and config.get("ib_gateway") is not None:
        logger.warning(
            "YAML key ib_gateway is deprecated; rename to ib_operator (same sub-keys supported)."
        )
        raw = config.get("ib_gateway")
    raw = raw if isinstance(raw, dict) else {}
    rurl = ib_redis_url_from_config(config)
    explicit_enabled = raw.get("enabled")
    if explicit_enabled is False:
        enabled = False
    elif explicit_enabled is True:
        enabled = True
    else:
        enabled = rurl is not None

    # Health hash is always the Socket Services canonical key (never ib:operator:meta:health).
    canonical_hk = BIFROST_HEALTH_IB_OPERATOR
    raw_hk = raw.get("health_key")
    hk = canonical_hk
    if isinstance(raw_hk, str) and raw_hk.strip():
        rs = raw_hk.strip()
        if rs == canonical_hk:
            pass
        elif rs in (LEGACY_IB_OPERATOR_META_HEALTH, LEGACY_BIFROST_IB_OPERATOR):
            logger.warning(
                "ib_operator.health_key=%s is deprecated; health is written only to %s",
                rs,
                canonical_hk,
            )
        else:
            logger.warning(
                "ib_operator.health_key=%r ignored; health is written only to %s",
                rs,
                canonical_hk,
            )

    return {
        "enabled": bool(enabled),
        "redis_url": rurl,
        "stream": (raw.get("stream") or "ib:operator:cmd").strip() or "ib:operator:cmd",
        "consumer_group": (raw.get("consumer_group") or "ib-operator").strip() or "ib-operator",
        "result_prefix": (raw.get("result_prefix") or "ib:operator:result:").strip()
        or "ib:operator:result:",
        "health_key": hk,
        "result_ttl_sec": int(raw.get("result_ttl_sec") or 300),
        "request_timeout_sec": float(raw.get("request_timeout_sec") or 120),
        # Celery bars backfill (fetch_bars_range RPC) can run for a long time; separate from default 120s API calls.
        "bars_backfill_request_timeout_sec": float(raw.get("bars_backfill_request_timeout_sec") or 7200),
        # Idle operator process only refreshes Redis on this interval (unlike ingestor, which updates on ticks).
        "health_refresh_sec": float(raw.get("health_refresh_sec") or 30),
        # Large range backfills return many bar rows in one RPC; default 4 MiB (override in YAML if needed).
        "max_result_bytes": int(raw.get("max_result_bytes") or (4 * 1024 * 1024)),
        "block_ms": int(raw.get("block_ms") or 5000),
        # TIBM3: default True — Celery bars use Platform IB Gateway fetch_bars_range RPC.
        # Explicit false is deprecated (direct TWS removed from worker); still honored as False.
        "use_for_celery_bars": (
            bool(raw["use_for_celery_bars"])
            if "use_for_celery_bars" in raw
            else True
        ),
    }


def redis_client_kwargs_from_config(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Sync redis.from_url kwargs for IB bus when redis_ib or redis is enabled; else None."""
    rurl = ib_redis_url_from_config(config)
    if not rurl:
        return None
    eff = effective_ib_redis_dict(config, default_db=0)
    return {"url": format_redis_url(eff), "decode_responses": True}
