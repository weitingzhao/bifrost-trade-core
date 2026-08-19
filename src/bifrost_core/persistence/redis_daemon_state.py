"""Daemon IPC state in per-env Redis (replaces PG daemon_* / account_sync_* IPC tables).

Keys (per-env redis from config ``redis`` block):
  bifrost:daemon:trading:state       HASH  TTL 180s
  bifrost:daemon:trading:control     STREAM MAXLEN ~500
  bifrost:daemon:account_sync:state  HASH  TTL 180s
  bifrost:daemon:account_sync:control STREAM MAXLEN ~100
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_TTL_SEC = 180

TRADING_STATE_KEY = "bifrost:daemon:trading:state"
TRADING_CONTROL_STREAM = "bifrost:daemon:trading:control"
TRADING_CONTROL_GROUP = "trading_daemon"
TRADING_CONTROL_CONSUMER = "daemon_0"
TRADING_CONTROL_MAXLEN = 500

ACCOUNT_SYNC_STATE_KEY = "bifrost:daemon:account_sync:state"
ACCOUNT_SYNC_CONTROL_STREAM = "bifrost:daemon:account_sync:control"
ACCOUNT_SYNC_CONTROL_GROUP = "account_sync_daemon"
ACCOUNT_SYNC_CONTROL_CONSUMER = "daemon_0"
ACCOUNT_SYNC_CONTROL_MAXLEN = 100

# Fields treated as bool when reading HASH
_BOOL_FIELDS = frozenset(
    {
        "hedge_running",
        "ib_connected",
        "redis_quotes_connected",
        "mock_hedging",
        "suspended",
        "alive",
    }
)
_INT_FIELDS = frozenset(
    {
        "ib_client_id",
        "heartbeat_interval_sec",
        "stock_position",
        "option_legs_count",
        "daily_hedge_count",
        "last_sync_version",
        "accounts_synced",
        "positions_synced",
        "executions_synced",
        "open_orders_synced",
        "stream_lag",
    }
)
_FLOAT_FIELDS = frozenset(
    {
        "last_ts",
        "graceful_shutdown_at",
        "spot",
        "bid",
        "ask",
        "net_delta",
        "daily_pnl",
        "data_lag_ms",
        "ts",
        "updated_at",
    }
)


def connect_daemon_state_redis(config: Optional[Dict[str, Any]]) -> Optional[Any]:
    """Open per-env redis client for daemon IPC. Returns None if redis not enabled."""
    try:
        import redis as redis_lib

        from bifrost_core.core.redis_url import redis_url_from_config

        url = redis_url_from_config(config or {})
        if not url:
            return None
        r = redis_lib.from_url(url, decode_responses=True)
        r.ping()
        return r
    except Exception as e:
        logger.warning("connect_daemon_state_redis failed: %s", e)
        return None


def _to_redis_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (list, dict)):
        return json.dumps(v, separators=(",", ":"), default=str)
    return str(v)


def _encode_mapping(fields: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in fields.items():
        if v is None and k not in ("subscribed_tickers", "last_control_message"):
            # Skip None for most fields so HSET does not wipe with empty unless explicit.
            continue
        out[k] = _to_redis_str(v)
    return out


def _decode_value(key: str, raw: str) -> Any:
    if raw == "" and key not in ("last_control_message", "subscribed_tickers", "symbol", "config_summary", "daemon_state", "trading_state"):
        return None
    if key == "subscribed_tickers":
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if key in _BOOL_FIELDS:
        return raw in ("1", "true", "True", "yes")
    if key in _INT_FIELDS:
        try:
            return int(float(raw)) if raw != "" else None
        except (TypeError, ValueError):
            return None
    if key in _FLOAT_FIELDS:
        try:
            return float(raw) if raw != "" else None
        except (TypeError, ValueError):
            return None
    return raw


def _decode_hash(raw: Dict[str, str]) -> Dict[str, Any]:
    return {k: _decode_value(k, v) for k, v in (raw or {}).items()}


def write_trading_daemon_state(r: Any, fields: Dict[str, Any]) -> bool:
    """HSET trading state HASH and refresh TTL."""
    if r is None or not fields:
        return False
    try:
        mapping = _encode_mapping(fields)
        if not mapping:
            return False
        pipe = r.pipeline()
        pipe.hset(TRADING_STATE_KEY, mapping=mapping)
        pipe.expire(TRADING_STATE_KEY, STATE_TTL_SEC)
        pipe.execute()
        return True
    except Exception as e:
        logger.warning("write_trading_daemon_state failed: %s", e)
        return False


def read_trading_daemon_state(r: Any) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    try:
        raw = r.hgetall(TRADING_STATE_KEY)
        if not raw:
            return None
        return _decode_hash(raw)
    except Exception as e:
        logger.debug("read_trading_daemon_state failed: %s", e)
        return None


def write_account_sync_state(r: Any, fields: Dict[str, Any]) -> bool:
    if r is None or not fields:
        return False
    try:
        mapping = _encode_mapping(fields)
        if not mapping:
            return False
        pipe = r.pipeline()
        pipe.hset(ACCOUNT_SYNC_STATE_KEY, mapping=mapping)
        pipe.expire(ACCOUNT_SYNC_STATE_KEY, STATE_TTL_SEC)
        pipe.execute()
        return True
    except Exception as e:
        logger.warning("write_account_sync_state failed: %s", e)
        return False


def read_account_sync_state(r: Any) -> Optional[Dict[str, Any]]:
    if r is None:
        return None
    try:
        raw = r.hgetall(ACCOUNT_SYNC_STATE_KEY)
        if not raw:
            return None
        return _decode_hash(raw)
    except Exception as e:
        logger.debug("read_account_sync_state failed: %s", e)
        return None


def publish_control(
    r: Any,
    stream_key: str,
    command: str,
    *,
    source: str = "api",
    maxlen: int = TRADING_CONTROL_MAXLEN,
) -> bool:
    """XADD control command to stream."""
    if r is None:
        return False
    cmd = (command or "").strip()
    if not cmd:
        return False
    try:
        r.xadd(
            stream_key,
            {
                "command": cmd,
                "source": source,
                "created_at": str(time.time()),
            },
            maxlen=maxlen,
            approximate=True,
        )
        return True
    except Exception as e:
        logger.warning("publish_control(%s) failed: %s", stream_key, e)
        return False


def publish_trading_control(r: Any, command: str, *, source: str = "api") -> bool:
    return publish_control(
        r, TRADING_CONTROL_STREAM, command, source=source, maxlen=TRADING_CONTROL_MAXLEN
    )


def publish_account_sync_control(r: Any, command: str, *, source: str = "api") -> bool:
    return publish_control(
        r,
        ACCOUNT_SYNC_CONTROL_STREAM,
        command,
        source=source,
        maxlen=ACCOUNT_SYNC_CONTROL_MAXLEN,
    )


def ensure_control_group(r: Any, stream_key: str, group: str) -> None:
    """Create consumer group if missing.

    Start from id ``0`` so commands published before the daemon starts are still
    delivered (stream is maxlen-capped; backlog is small).
    """
    if r is None:
        return
    try:
        r.xgroup_create(stream_key, group, id="0", mkstream=True)
    except Exception as e:
        msg = str(e).upper()
        if "BUSYGROUP" not in msg and "BUSY" not in msg:
            logger.debug("ensure_control_group %s/%s: %s", stream_key, group, e)


def consume_control(
    r: Any,
    stream_key: str,
    group: str,
    consumer: str,
    *,
    block_ms: int = 0,
    count: int = 1,
) -> Optional[str]:
    """XREADGROUP one command; ACK and return command string, or None."""
    if r is None:
        return None
    try:
        ensure_control_group(r, stream_key, group)
        # Drain pending (unacked from a previous crash) before new messages.
        rows = r.xreadgroup(
            groupname=group,
            consumername=consumer,
            streams={stream_key: "0"},
            count=count,
            block=None,
        )
        if not rows:
            rows = r.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream_key: ">"},
                count=count,
                block=block_ms if block_ms > 0 else None,
            )
        if not rows:
            return None
        # rows: [[stream_name, [(id, {fields}), ...]]]
        for _stream, messages in rows:
            for msg_id, fields in messages:
                cmd = (fields.get("command") or "").strip()
                try:
                    r.xack(stream_key, group, msg_id)
                except Exception:
                    pass
                if cmd:
                    return cmd
        return None
    except Exception as e:
        logger.debug("consume_control(%s) failed: %s", stream_key, e)
        return None


def consume_trading_control(r: Any, *, block_ms: int = 0) -> Optional[str]:
    return consume_control(
        r,
        TRADING_CONTROL_STREAM,
        TRADING_CONTROL_GROUP,
        TRADING_CONTROL_CONSUMER,
        block_ms=block_ms,
    )


def consume_account_sync_control(r: Any, *, block_ms: int = 0) -> Optional[str]:
    return consume_control(
        r,
        ACCOUNT_SYNC_CONTROL_STREAM,
        ACCOUNT_SYNC_CONTROL_GROUP,
        ACCOUNT_SYNC_CONTROL_CONSUMER,
        block_ms=block_ms,
    )


def set_trading_run_status(
    r: Any, *, suspended: Optional[bool] = None, heartbeat_interval_sec: Optional[float] = None
) -> bool:
    fields: Dict[str, Any] = {}
    if suspended is not None:
        fields["suspended"] = suspended
    if heartbeat_interval_sec is not None:
        fields["heartbeat_interval_sec"] = max(5, min(120, int(heartbeat_interval_sec)))
    return write_trading_daemon_state(r, fields)


def set_account_sync_run_status(
    r: Any, *, suspended: Optional[bool] = None, heartbeat_interval_sec: Optional[float] = None
) -> bool:
    fields: Dict[str, Any] = {}
    if suspended is not None:
        fields["suspended"] = suspended
    if heartbeat_interval_sec is not None:
        fields["heartbeat_interval_sec"] = max(2.0, min(60.0, float(heartbeat_interval_sec)))
    return write_account_sync_state(r, fields)


def trading_heartbeat_from_state(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Shape matching get_daemon_heartbeat() historical output."""
    if not state:
        return None
    return {
        "last_ts": state.get("last_ts"),
        "hedge_running": bool(state.get("hedge_running", False)),
        "ib_connected": bool(state.get("ib_connected", False)),
        "ib_client_id": state.get("ib_client_id"),
        "next_retry_ts": None,
        "seconds_until_retry": None,
        "graceful_shutdown_at": state.get("graceful_shutdown_at"),
        "heartbeat_interval_sec": state.get("heartbeat_interval_sec"),
        "redis_quotes_connected": bool(state.get("redis_quotes_connected", False)),
        "last_control_message": state.get("last_control_message") or None,
        "subscribed_tickers": state.get("subscribed_tickers") or [],
        "mock_hedging": bool(state.get("mock_hedging", True)),
    }


def trading_status_current_from_state(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Shape matching daemon_auto_status_current row."""
    if not state:
        return None
    # Require at least daemon_state or ts to treat as a status snapshot
    if state.get("daemon_state") is None and state.get("ts") is None:
        return None
    return {
        "daemon_auto_status_current_id": 1,
        "daemon_state": state.get("daemon_state"),
        "trading_state": state.get("trading_state"),
        "symbol": state.get("symbol"),
        "spot": state.get("spot"),
        "bid": state.get("bid"),
        "ask": state.get("ask"),
        "net_delta": state.get("net_delta"),
        "stock_position": state.get("stock_position"),
        "option_legs_count": state.get("option_legs_count"),
        "daily_hedge_count": state.get("daily_hedge_count"),
        "daily_pnl": state.get("daily_pnl"),
        "data_lag_ms": state.get("data_lag_ms"),
        "config_summary": state.get("config_summary"),
        "ts": state.get("ts"),
    }


def account_sync_heartbeat_from_state(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not state:
        return None
    if state.get("last_ts") is None and state.get("alive") is None:
        return None
    hi = state.get("heartbeat_interval_sec")
    return {
        "last_ts": state.get("last_ts"),
        "last_sync_version": state.get("last_sync_version") or 0,
        "accounts_synced": state.get("accounts_synced") or 0,
        "positions_synced": state.get("positions_synced") or 0,
        "executions_synced": state.get("executions_synced") or 0,
        "open_orders_synced": state.get("open_orders_synced") or 0,
        "stream_lag": state.get("stream_lag") or 0,
        "heartbeat_interval_sec": float(hi) if hi is not None else 5.0,
        "suspended": bool(state.get("suspended", False)),
        "alive": bool(state.get("alive", True)),
    }
