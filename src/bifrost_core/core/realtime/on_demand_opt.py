"""On-demand OPT subscription control for IB Gateway option cache (Market Live).

Redis contract:
- SET ``ib:option:control:on_demand_opt`` — OPT contract_key values
- HASH ``ib:option:control:on_demand_opt_ts`` — field=contract_key → unix heartbeat ts
- STRING ``ib:option:cache:{contract_key}`` — JSON quote (TTL) written by Gateway

Market API refreshes heartbeats on ``GET /quotes``; Gateway one-shot loop
fetches fresh keys and writes cache. ``remove_on_demand_opt`` explicitly
unregisters (SREM + HDEL + DEL cache keys).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, List, Optional, Sequence

from bifrost_core.core.realtime.ib_ingestor_keys import (
    IB_OPTION_CACHE_PREFIX,
    IB_OPTION_ON_DEMAND_SET,
    IB_OPTION_ON_DEMAND_TS,
    ON_DEMAND_OPT_DEFAULT_MAX_AGE_SEC,
)

logger = logging.getLogger(__name__)


def normalize_opt_contract_keys(keys: Iterable[str]) -> List[str]:
    """Normalize unique OPT contract_keys (order preserved).

    Expected format: ``SYMBOL|OPT|YYYYMMDD|STRIKE|RIGHT`` (e.g. ``GOOG|OPT|20260717|300.0|C``).
    """
    out: List[str] = []
    seen: set[str] = set()
    for raw in keys:
        ck = str(raw or "").strip()
        if not ck:
            continue
        parts = ck.split("|")
        if len(parts) != 5:
            continue
        sym, sec, expiry, strike_raw, right = parts
        sym = sym.strip().upper()
        sec = sec.strip().upper()
        expiry = expiry.strip()
        strike = strike_raw.strip()
        right = right.strip().upper()
        if not sym or sec != "OPT" or not expiry or right not in ("C", "P"):
            continue
        if not expiry.isdigit() or len(expiry) < 6:
            continue
        try:
            float(strike)
        except (TypeError, ValueError):
            continue
        normalized = f"{sym}|OPT|{expiry}|{strike}|{right}"
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def ensure_on_demand_opt(
    client: Any,
    contract_keys: Sequence[str],
    *,
    now: Optional[float] = None,
) -> List[str]:
    """Register OPT contract_keys for on-demand cache refresh (SADD + heartbeat HSET).

    Returns the normalized keys that were registered. No-op on empty list.
    """
    if client is None:
        return []
    keys = normalize_opt_contract_keys(contract_keys)
    if not keys:
        return []
    ts = float(now if now is not None else time.time())
    pipe = client.pipeline(transaction=False)
    pipe.sadd(IB_OPTION_ON_DEMAND_SET, *keys)
    mapping = {k: str(ts) for k in keys}
    pipe.hset(IB_OPTION_ON_DEMAND_TS, mapping=mapping)
    pipe.execute()
    return keys


def remove_on_demand_opt(client: Any, contract_keys: Sequence[str]) -> int:
    """Explicit unregister: SREM + HDEL heartbeat + DEL option cache keys.

    Returns the number of normalized keys processed for removal.
    """
    if client is None:
        return 0
    keys = normalize_opt_contract_keys(contract_keys)
    if not keys:
        return 0
    pipe = client.pipeline(transaction=False)
    pipe.srem(IB_OPTION_ON_DEMAND_SET, *keys)
    pipe.hdel(IB_OPTION_ON_DEMAND_TS, *keys)
    for ck in keys:
        pipe.delete(f"{IB_OPTION_CACHE_PREFIX}{ck}")
    pipe.execute()
    return len(keys)


def list_fresh_on_demand_opt(
    client: Any,
    *,
    max_age_sec: float = ON_DEMAND_OPT_DEFAULT_MAX_AGE_SEC,
    now: Optional[float] = None,
) -> List[str]:
    """Return on-demand OPT keys with fresh heartbeats; prune expired SET/HASH members."""
    if client is None:
        return []
    ts_now = float(now if now is not None else time.time())
    max_age = float(max_age_sec)
    try:
        members = client.smembers(IB_OPTION_ON_DEMAND_SET) or set()
    except Exception as e:
        logger.warning("list_fresh_on_demand_opt SMEMBERS failed: %s", e)
        return []
    if not members:
        return []

    try:
        ts_map = client.hgetall(IB_OPTION_ON_DEMAND_TS) or {}
    except Exception as e:
        logger.warning("list_fresh_on_demand_opt HGETALL failed: %s", e)
        ts_map = {}

    fresh: List[str] = []
    stale: List[str] = []
    for raw in members:
        ck = str(raw or "").strip()
        if not ck:
            continue
        raw_ts = ts_map.get(ck) if isinstance(ts_map, dict) else None
        if raw_ts is None and isinstance(ts_map, dict):
            raw_ts = ts_map.get(ck.encode()) if hasattr(ck, "encode") else None
        try:
            last = float(raw_ts) if raw_ts is not None else 0.0
        except (TypeError, ValueError):
            last = 0.0
        if last > 0 and (ts_now - last) <= max_age:
            fresh.append(ck)
        else:
            stale.append(ck)

    if stale:
        try:
            client.srem(IB_OPTION_ON_DEMAND_SET, *stale)
            client.hdel(IB_OPTION_ON_DEMAND_TS, *stale)
        except Exception as e:
            logger.warning("list_fresh_on_demand_opt prune failed: %s", e)

    fresh.sort()
    return fresh
