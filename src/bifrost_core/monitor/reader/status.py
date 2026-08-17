"""Status, operations, daemon_heartbeat, daemon_run_status, control commands, risk summary.

Daemon heartbeat read and row-to-dict conversion live only in this module; do not duplicate
in other modules. All callers (e.g. StatusReader in common.py) must use get_daemon_heartbeat(conn)."""

import logging
import time
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.brokerage_tables import OPEN_ORDERS
from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)

# Row layout from SELECT (no legacy listener / event_subscribe columns):
# 0-8 core + redis_quotes_connected, 9 last_control_message, 10 subscribed_tickers, 11 mock_hedging


def _row_to_heartbeat(row: tuple) -> Dict[str, Any]:
    """Build daemon_heartbeat dict from compact SELECT (see module docstring)."""
    out: Dict[str, Any] = {
        "last_ts": float(row[0]) if row[0] is not None else None,
        "hedge_running": bool(row[1]),
        "ib_connected": bool(row[2]) if row[2] is not None else False,
        "ib_client_id": int(row[3]) if row[3] is not None else None,
        "next_retry_ts": float(row[4]) if row[4] is not None else None,
        "seconds_until_retry": int(row[5]) if row[5] is not None else None,
        "graceful_shutdown_at": float(row[6]) if len(row) > 6 and row[6] is not None else None,
        "heartbeat_interval_sec": int(row[7]) if len(row) > 7 and row[7] is not None else None,
        "redis_quotes_connected": bool(row[8]) if len(row) > 8 and row[8] is not None else False,
    }
    if len(row) > 9 and row[9] is not None:
        s = str(row[9]).strip()
        out["last_control_message"] = s if s else None
    else:
        out["last_control_message"] = None
    if len(row) > 10:
        r10 = row[10]
        if r10 is not None:
            out["subscribed_tickers"] = (
                list(r10) if hasattr(r10, "__iter__") and not isinstance(r10, str) else []
            )
        else:
            out["subscribed_tickers"] = None
    else:
        out["subscribed_tickers"] = None
    if len(row) > 11 and row[11] is not None:
        out["mock_hedging"] = bool(row[11])
    else:
        out["mock_hedging"] = True
    return out


def get_status_current(conn: Any) -> Optional[Dict[str, Any]]:
    """Return the single row from daemon_auto_status_current as a dict, or None if empty/unavailable.
    Row keys include daemon_auto_status_current_id (PK), daemon_state, trading_state, symbol, spot, ts, etc."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM daemon_auto_status_current WHERE daemon_auto_status_current_id = 1")
            row = cur.fetchone()
        if row is None:
            return None
        return dict(row)
    except Exception as e:
        logger.warning("get_status_current failed: %s", e)
        return None


def get_run_status(conn: Any) -> Optional[bool]:
    """Return daemon_run_status.suspended for row id=1. None if table missing or unavailable."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT suspended FROM daemon_run_status WHERE id = 1")
            row = cur.fetchone()
        if row is None:
            return None
        return bool(row[0])
    except Exception as e:
        logger.debug("get_run_status failed: %s", e)
        return None


def get_daemon_heartbeat(conn: Any) -> Optional[Dict[str, Any]]:
    """Return daemon_heartbeat row id=1. None if table missing.
    Tries compact column sets in order (newest schema first); pads tuple for _row_to_heartbeat."""
    attempts: List[tuple[str, str, tuple]] = [
        (
            "full",
            """
            SELECT extract(epoch from last_ts) AS last_ts, hedge_running,
                   ib_connected, ib_client_id,
                   extract(epoch from next_retry_ts) AS next_retry_ts,
                   seconds_until_retry,
                   extract(epoch from graceful_shutdown_at) AS graceful_shutdown_at,
                   heartbeat_interval_sec,
                   redis_quotes_connected,
                   last_control_message,
                   subscribed_tickers,
                   mock_hedging
            FROM daemon_heartbeat WHERE id = 1
            """,
            (),
        ),
        (
            "no_mock_hedging",
            """
            SELECT extract(epoch from last_ts) AS last_ts, hedge_running,
                   ib_connected, ib_client_id,
                   extract(epoch from next_retry_ts) AS next_retry_ts,
                   seconds_until_retry,
                   extract(epoch from graceful_shutdown_at) AS graceful_shutdown_at,
                   heartbeat_interval_sec,
                   redis_quotes_connected,
                   last_control_message,
                   subscribed_tickers
            FROM daemon_heartbeat WHERE id = 1
            """,
            (True,),
        ),
        (
            "no_subscribed_tickers",
            """
            SELECT extract(epoch from last_ts) AS last_ts, hedge_running,
                   ib_connected, ib_client_id,
                   extract(epoch from next_retry_ts) AS next_retry_ts,
                   seconds_until_retry,
                   extract(epoch from graceful_shutdown_at) AS graceful_shutdown_at,
                   heartbeat_interval_sec,
                   redis_quotes_connected,
                   last_control_message
            FROM daemon_heartbeat WHERE id = 1
            """,
            (None, True),
        ),
        (
            "no_last_control_message",
            """
            SELECT extract(epoch from last_ts) AS last_ts, hedge_running,
                   ib_connected, ib_client_id,
                   extract(epoch from next_retry_ts) AS next_retry_ts,
                   seconds_until_retry,
                   extract(epoch from graceful_shutdown_at) AS graceful_shutdown_at,
                   heartbeat_interval_sec,
                   redis_quotes_connected
            FROM daemon_heartbeat WHERE id = 1
            """,
            (None, None, True),
        ),
        (
            "no_redis_quotes_connected",
            """
            SELECT extract(epoch from last_ts) AS last_ts, hedge_running,
                   ib_connected, ib_client_id,
                   extract(epoch from next_retry_ts) AS next_retry_ts,
                   seconds_until_retry,
                   extract(epoch from graceful_shutdown_at) AS graceful_shutdown_at,
                   heartbeat_interval_sec
            FROM daemon_heartbeat WHERE id = 1
            """,
            (False, None, None, True),
        ),
        (
            "minimal_core",
            """
            SELECT extract(epoch from last_ts) AS last_ts, hedge_running,
                   ib_connected, ib_client_id,
                   extract(epoch from next_retry_ts) AS next_retry_ts,
                   seconds_until_retry
            FROM daemon_heartbeat WHERE id = 1
            """,
            (None, None, False, None, None, True),
        ),
    ]
    last_err: Optional[Exception] = None
    for label, sql, pad in attempts:
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
            if row is None:
                return None
            if pad:
                row = row + pad
            return _row_to_heartbeat(row)
        except Exception as e:
            last_err = e
            logger.debug("get_daemon_heartbeat try %s failed: %s", label, e)
            continue
    if last_err is not None:
        logger.debug("get_daemon_heartbeat failed after all attempts: %s", last_err)
    return None


def get_operations(
    conn: Any,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    type_filter: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return rows from daemon_auto_operations, optionally filtered by time and type. Newest first.
    Each row dict includes daemon_auto_operations_id (PK), ts, type, side, quantity, price, state_reason."""
    try:
        conditions = []
        values: List[Any] = []
        if since_ts is not None:
            conditions.append("ts >= %s")
            values.append(since_ts)
        if until_ts is not None:
            conditions.append("ts <= %s")
            values.append(until_ts)
        if type_filter is not None:
            conditions.append("type = %s")
            values.append(type_filter)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        values.append(limit)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT * FROM daemon_auto_operations{where} ORDER BY ts DESC LIMIT %s",
                values,
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("get_operations failed: %s", e)
        return []


def get_open_orders(conn: Any) -> List[Dict[str, Any]]:
    """R-A5: Return current open orders from daemon_open_orders (symbol, action, status, filled, remaining, limit_price, etc.)."""
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
    """Insert a control command (stop/flatten) into daemon_control table. Returns True on success."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        params.setdefault("connect_timeout", 5)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO daemon_control (command) VALUES (%s)", (command.strip().lower(),))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_control_command failed: %s", e)
        return False


def write_run_status(status_config: dict, suspended: bool) -> bool:
    """Update daemon_run_status row id=1 (suspended=true/false). Returns True on success."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO daemon_run_status (id, suspended, updated_at)
                    VALUES (1, %s, now())
                    ON CONFLICT (id) DO UPDATE SET suspended = %s, updated_at = now()
                    """,
                    (suspended, suspended),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_run_status failed: %s", e)
        return False


def write_heartbeat_interval(status_config: dict, heartbeat_interval_sec: int) -> bool:
    """Update daemon_run_status.heartbeat_interval_sec for row id=1 (clamped 5-120). Returns True on success."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    sec = max(5, min(120, heartbeat_interval_sec))
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE daemon_run_status SET heartbeat_interval_sec = %s, updated_at = now() WHERE id = 1",
                    (sec,),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_heartbeat_interval failed: %s", e)
        return False


def get_account_sync_heartbeat(conn: Any) -> Optional[Dict[str, Any]]:
    """Return account_sync_heartbeat row id=1. None if table missing."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT extract(epoch from h.last_ts) AS last_ts,
                       h.last_sync_version, h.accounts_synced, h.positions_synced,
                       h.executions_synced, h.open_orders_synced, h.stream_lag,
                       COALESCE(rs.heartbeat_interval_sec, 5.0) AS heartbeat_interval_sec
                FROM account_sync_heartbeat h
                LEFT JOIN account_sync_run_status rs ON rs.id = 1
                WHERE h.id = 1
                """
            )
            row = cur.fetchone()
            if not row:
                return None
            # extract(epoch) may be Decimal from psycopg2; coerce for time arithmetic / JSON.
            _lts = row[0]
            _hi = row[7]
            return {
                "last_ts": float(_lts) if _lts is not None else None,
                "last_sync_version": row[1],
                "accounts_synced": row[2],
                "positions_synced": row[3],
                "executions_synced": row[4],
                "open_orders_synced": row[5],
                "stream_lag": row[6],
                "heartbeat_interval_sec": float(_hi) if _hi is not None else 5.0,
            }
    except Exception:
        return None


def write_account_sync_control(status_config: dict, command: str) -> bool:
    """Insert a command into account_sync_control."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO account_sync_control (command) VALUES (%s)", (command,))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_account_sync_control failed: %s", e)
        return False


def write_account_sync_run_status(status_config: dict, *, suspended: bool) -> bool:
    """Update account_sync_run_status row id=1."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO account_sync_run_status (id, suspended, updated_at)
                    VALUES (1, %s, now())
                    ON CONFLICT (id) DO UPDATE SET suspended = %s, updated_at = now()
                    """,
                    (suspended, suspended),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_account_sync_run_status failed: %s", e)
        return False


def write_account_sync_heartbeat_interval(status_config: dict, interval_sec: float) -> bool:
    """Update account_sync_run_status.heartbeat_interval_sec (clamped 2-60)."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    sec = max(2.0, min(60.0, float(interval_sec)))
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE account_sync_run_status SET heartbeat_interval_sec = %s, updated_at = now() WHERE id = 1",
                    (sec,),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_account_sync_heartbeat_interval failed: %s", e)
        return False


def get_risk_summary(conn: Any) -> Dict[str, Any]:
    """Return risk/post-mortem summary: daemon_auto_status_current (daily_hedge_count, daily_pnl) + daemon_auto_operations count in last 24h + block_reasons."""
    out: Dict[str, Any] = {
        "daily_hedge_count": None,
        "daily_pnl": None,
        "spot": None,
        "symbol": None,
        "operations_count_24h": 0,
        "block_reasons": [],
        "ts": None,
    }
    row = get_status_current(conn)
    if row is not None:
        out["daily_hedge_count"] = row.get("daily_hedge_count")
        out["daily_pnl"] = row.get("daily_pnl")
        out["spot"] = row.get("spot")
        out["symbol"] = row.get("symbol")
        out["ts"] = row.get("ts")
    now = time.time()
    ops = get_operations(conn, since_ts=now - 86400, limit=500)
    out["operations_count_24h"] = len(ops)
    return out
