"""On-demand STK subscription control for IB Gateway / Ingestor (Market Live).

Redis contract:
- SET ``ib:ingester:control:on_demand_stk`` — uppercase symbols
- HASH ``ib:ingester:control:on_demand_stk_ts`` — field=SYM → unix heartbeat ts

Market API refreshes heartbeats on ``GET /quotes``; Gateway/Ingestor merges
fresh symbols into Host ``reqMktData`` and prunes stale ones.
``remove_on_demand_stk`` / ``POST /quotes/cleanup`` explicitly unsubscribe
(SREM + HDEL + DEL tick keys).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, List, Optional, Sequence

from bifrost_core.core.realtime.ib_ingestor_keys import (
    IB_INGESTER_ON_DEMAND_STK,
    IB_INGESTER_ON_DEMAND_STK_TS,
    IB_INGESTER_TICK_PREFIX,
    ON_DEMAND_STK_DEFAULT_MAX_AGE_SEC,
)

logger = logging.getLogger(__name__)


def normalize_stk_symbols(symbols: Iterable[str]) -> List[str]:
    """Uppercase unique STK tickers (order preserved)."""
    out: List[str] = []
    seen: set[str] = set()
    for raw in symbols:
        sym = str(raw or "").strip().upper()
        if not sym or sym in seen:
            continue
        # Skip OPT contract keys accidentally passed as symbols
        if "|" in sym:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def ensure_on_demand_stk(
    client: Any,
    symbols: Sequence[str],
    *,
    now: Optional[float] = None,
) -> List[str]:
    """Register STK symbols for on-demand market data (SADD + heartbeat HSET).

    Returns the normalized symbols that were registered. No-op on empty list.
    Raises only if Redis ops fail (caller may catch).
    """
    if client is None:
        return []
    syms = normalize_stk_symbols(symbols)
    if not syms:
        return []
    ts = float(now if now is not None else time.time())
    pipe = client.pipeline(transaction=False)
    pipe.sadd(IB_INGESTER_ON_DEMAND_STK, *syms)
    mapping = {s: str(ts) for s in syms}
    pipe.hset(IB_INGESTER_ON_DEMAND_STK_TS, mapping=mapping)
    pipe.execute()
    return syms


def remove_on_demand_stk(client: Any, symbols: Sequence[str]) -> int:
    """Explicit unsubscribe: SREM + HDEL heartbeat + DEL tick keys.

    Returns the number of normalized symbols processed for removal.
    No-op on empty list or ``client is None``.
    """
    if client is None:
        return 0
    syms = normalize_stk_symbols(symbols)
    if not syms:
        return 0
    pipe = client.pipeline(transaction=False)
    pipe.srem(IB_INGESTER_ON_DEMAND_STK, *syms)
    pipe.hdel(IB_INGESTER_ON_DEMAND_STK_TS, *syms)
    for sym in syms:
        pipe.delete(f"{IB_INGESTER_TICK_PREFIX}{sym}|STK|||")
    pipe.execute()
    return len(syms)


def list_fresh_on_demand_stk(
    client: Any,
    *,
    max_age_sec: float = ON_DEMAND_STK_DEFAULT_MAX_AGE_SEC,
    now: Optional[float] = None,
) -> List[str]:
    """Return on-demand symbols with fresh heartbeats; prune expired SET/HASH members."""
    if client is None:
        return []
    ts_now = float(now if now is not None else time.time())
    max_age = float(max_age_sec)
    try:
        members = client.smembers(IB_INGESTER_ON_DEMAND_STK) or set()
    except Exception as e:
        logger.warning("list_fresh_on_demand_stk SMEMBERS failed: %s", e)
        return []
    if not members:
        return []

    try:
        ts_map = client.hgetall(IB_INGESTER_ON_DEMAND_STK_TS) or {}
    except Exception as e:
        logger.warning("list_fresh_on_demand_stk HGETALL failed: %s", e)
        ts_map = {}

    fresh: List[str] = []
    stale: List[str] = []
    for raw in members:
        sym = str(raw or "").strip().upper()
        if not sym:
            continue
        raw_ts = ts_map.get(sym) if isinstance(ts_map, dict) else None
        # redis-py may return bytes keys if decode_responses=False
        if raw_ts is None and isinstance(ts_map, dict):
            raw_ts = ts_map.get(sym.encode()) if hasattr(sym, "encode") else None
        try:
            last = float(raw_ts) if raw_ts is not None else 0.0
        except (TypeError, ValueError):
            last = 0.0
        if last > 0 and (ts_now - last) <= max_age:
            fresh.append(sym)
        else:
            stale.append(sym)

    if stale:
        try:
            client.srem(IB_INGESTER_ON_DEMAND_STK, *stale)
            client.hdel(IB_INGESTER_ON_DEMAND_STK_TS, *stale)
        except Exception as e:
            logger.warning("list_fresh_on_demand_stk prune failed: %s", e)

    fresh.sort()
    return fresh
