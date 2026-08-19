"""Status, daemon heartbeat/run_status/control via Redis daemon IPC; open orders still from PG.

Daemon state lives in per-env Redis (see bifrost_core.persistence.redis_daemon_state).
"""

import logging
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.brokerage_tables import OPEN_ORDERS
from bifrost_core.persistence import redis_daemon_state as rds

logger = logging.getLogger(__name__)


def _redis_from_config(status_config: Optional[dict]) -> Optional[Any]:
    return rds.connect_daemon_state_redis(status_config or {})


def get_status_current(
    conn: Any = None, *, redis_client: Any = None, status_config: Optional[dict] = None
) -> Optional[Dict[str, Any]]:
    """Return trading status snapshot from Redis (shape of former daemon_auto_status_current)."""
    r = redis_client
    if r is None and status_config is not None:
        r = _redis_from_config(status_config)
    if r is None:
        return None
    return rds.trading_status_current_from_state(rds.read_trading_daemon_state(r))


def get_run_status(
    conn: Any = None, *, redis_client: Any = None, status_config: Optional[dict] = None
) -> Optional[bool]:
    """Return trading daemon suspended flag from Redis. None if state missing."""
    r = redis_client
    if r is None and status_config is not None:
        r = _redis_from_config(status_config)
    if r is None:
        return None
    state = rds.read_trading_daemon_state(r)
    if not state or "suspended" not in state:
        return None
    return bool(state.get("suspended"))


def get_daemon_heartbeat(
    conn: Any = None, *, redis_client: Any = None, status_config: Optional[dict] = None
) -> Optional[Dict[str, Any]]:
    """Return trading daemon heartbeat from Redis."""
    r = redis_client
    if r is None and status_config is not None:
        r = _redis_from_config(status_config)
    if r is None:
        return None
    return rds.trading_heartbeat_from_state(rds.read_trading_daemon_state(r))


def get_operations(
    conn: Any,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    type_filter: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """daemon_auto_operations retired (Wave 1). Always returns empty list."""
    return []


def get_open_orders(conn: Any) -> List[Dict[str, Any]]:
    """R-A5: Return current open orders from brokerage.open_orders."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT order_id, perm_id, account_id, symbol, sec_type, action,
                       total_quantity, filled, remaining, limit_price, status, contract_key,
                       extract(epoch from updated_ts) AS updated_ts
                FROM {OPEN_ORDERS}
                ORDER BY updated_ts DESC NULLS LAST
                """
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("get_open_orders failed: %s", e)
        return []


def write_control_command(status_config: dict, command: str) -> bool:
    """Publish trading daemon control command to Redis STREAM."""
    r = _redis_from_config(status_config)
    if r is None:
        return False
    return rds.publish_trading_control(r, command, source="api")


def write_run_status(status_config: dict, suspended: bool) -> bool:
    """Set trading daemon suspended flag on Redis state HASH."""
    r = _redis_from_config(status_config)
    if r is None:
        return False
    return rds.set_trading_run_status(r, suspended=suspended)


def write_heartbeat_interval(status_config: dict, heartbeat_interval_sec: int) -> bool:
    """Set trading daemon heartbeat_interval_sec on Redis state HASH (clamped 5-120)."""
    r = _redis_from_config(status_config)
    if r is None:
        return False
    return rds.set_trading_run_status(r, heartbeat_interval_sec=float(heartbeat_interval_sec))


def get_account_sync_heartbeat(
    conn: Any = None, *, redis_client: Any = None, status_config: Optional[dict] = None
) -> Optional[Dict[str, Any]]:
    """Return account sync heartbeat from Redis."""
    r = redis_client
    if r is None and status_config is not None:
        r = _redis_from_config(status_config)
    if r is None:
        return None
    return rds.account_sync_heartbeat_from_state(rds.read_account_sync_state(r))


def write_account_sync_control(status_config: dict, command: str) -> bool:
    """Publish account-sync control command to Redis STREAM."""
    r = _redis_from_config(status_config)
    if r is None:
        return False
    return rds.publish_account_sync_control(r, command, source="api")


def write_account_sync_run_status(status_config: dict, *, suspended: bool) -> bool:
    """Set account-sync suspended flag on Redis state HASH."""
    r = _redis_from_config(status_config)
    if r is None:
        return False
    return rds.set_account_sync_run_status(r, suspended=suspended)


def write_account_sync_heartbeat_interval(status_config: dict, interval_sec: float) -> bool:
    """Set account-sync heartbeat_interval_sec on Redis (clamped 2-60)."""
    r = _redis_from_config(status_config)
    if r is None:
        return False
    return rds.set_account_sync_run_status(r, heartbeat_interval_sec=float(interval_sec))


def get_risk_summary(conn: Any = None, *, status_config: Optional[dict] = None) -> Dict[str, Any]:
    """Return risk/post-mortem summary from Redis trading status."""
    out: Dict[str, Any] = {
        "daily_hedge_count": None,
        "daily_pnl": None,
        "spot": None,
        "symbol": None,
        "operations_count_24h": 0,
        "block_reasons": [],
        "ts": None,
    }
    row = get_status_current(status_config=status_config)
    if row is not None:
        out["daily_hedge_count"] = row.get("daily_hedge_count")
        out["daily_pnl"] = row.get("daily_pnl")
        out["spot"] = row.get("spot")
        out["symbol"] = row.get("symbol")
        out["ts"] = row.get("ts")
    return out
