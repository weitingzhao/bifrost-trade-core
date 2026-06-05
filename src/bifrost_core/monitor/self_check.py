"""Derive self_check and lamps for GET /status: daemon (heartbeat + auto-trading row), then health roll-up."""

from typing import Any, Dict, List, Optional

# Default data lag threshold (ms) when not in config
_DEFAULT_DATA_LAG_THRESHOLD_MS = 5000.0


def _auto_trading_self_check_from_row(
    row: Optional[Dict[str, Any]],
    data_lag_threshold_ms: Optional[float],
    trading_suspended: Optional[bool],
) -> Optional[Dict[str, Any]]:
    """Return None if trading row checks pass; else dict with daemon_self_check, daemon_lamp, daemon_block_reasons."""
    threshold = (
        data_lag_threshold_ms
        if data_lag_threshold_ms is not None
        else _DEFAULT_DATA_LAG_THRESHOLD_MS
    )
    block_reasons: List[str] = []

    if row is None:
        block_reasons.append("no_status")
        return {
            "daemon_self_check": "blocked",
            "daemon_block_reasons": block_reasons,
            "daemon_lamp": "red",
        }

    daemon_state = (row.get("daemon_state") or "").strip().upper()
    if daemon_state not in ("RUNNING", "RUNNING_SUSPENDED"):
        block_reasons.append("daemon_not_running")
        return {
            "daemon_self_check": "blocked",
            "daemon_block_reasons": block_reasons,
            "daemon_lamp": "red",
        }

    is_suspended = trading_suspended or (daemon_state == "RUNNING_SUSPENDED")
    if is_suspended:
        block_reasons.append("trading_suspended")
        return {
            "daemon_self_check": "degraded",
            "daemon_block_reasons": block_reasons,
            "daemon_lamp": "yellow",
        }

    data_lag_ms = row.get("data_lag_ms")
    if data_lag_ms is not None and float(data_lag_ms) > threshold:
        block_reasons.append("data_stale")
        return {
            "daemon_self_check": "degraded",
            "daemon_block_reasons": block_reasons,
            "daemon_lamp": "yellow",
        }

    trading_state = (row.get("trading_state") or "").strip().upper()
    if trading_state in ("PAUSE_COST", "RISK_HALT", "STALE", "FORCE_HEDGE"):
        block_reasons.append(f"trading_state_{trading_state.lower()}")
        return {
            "daemon_self_check": "degraded",
            "daemon_block_reasons": block_reasons,
            "daemon_lamp": "yellow",
        }

    return None


def derive_daemon_self_check(
    daemon_heartbeat: Optional[Dict[str, Any]],
    *,
    auto_status_row: Optional[Dict[str, Any]] = None,
    data_lag_threshold_ms: Optional[float] = None,
    trading_suspended: Optional[bool] = None,
) -> Dict[str, Any]:
    """Daemon roll-up: heartbeat (process + IB trading connection), then auto-trading row (FSM, lag, trading_state).

    Returns:
        {"daemon_self_check", "daemon_lamp", "daemon_block_reasons"}
    """
    if not daemon_heartbeat:
        return {
            "daemon_self_check": "blocked",
            "daemon_lamp": "red",
            "daemon_block_reasons": ["no_heartbeat"],
        }
    daemon_alive = daemon_heartbeat.get("daemon_alive", False)
    ib_connected = daemon_heartbeat.get("ib_connected", False)

    if not daemon_alive:
        last_ts = daemon_heartbeat.get("last_ts")
        reason = "heartbeat_stale" if last_ts is not None else "daemon_not_running"
        return {
            "daemon_self_check": "blocked",
            "daemon_lamp": "red",
            "daemon_block_reasons": [reason],
        }
    if not ib_connected:
        return {
            "daemon_self_check": "degraded",
            "daemon_lamp": "yellow",
            "daemon_block_reasons": ["ib_not_connected"],
        }

    bad = _auto_trading_self_check_from_row(
        auto_status_row, data_lag_threshold_ms, trading_suspended
    )
    if bad is not None:
        return {
            "daemon_self_check": bad["daemon_self_check"],
            "daemon_lamp": bad["daemon_lamp"],
            "daemon_block_reasons": bad["daemon_block_reasons"],
        }

    return {
        "daemon_self_check": "ok",
        "daemon_lamp": "green",
        "daemon_block_reasons": [],
    }


def _lamp_rank(lamp: str) -> int:
    x = (lamp or "red").strip().lower()
    if x == "red":
        return 3
    if x == "yellow":
        return 2
    return 1


def _rank_to_health(rank: int) -> Dict[str, Any]:
    if rank >= 3:
        return {
            "self_check": "blocked",
            "status_lamp": "red",
        }
    if rank >= 2:
        return {
            "self_check": "degraded",
            "status_lamp": "yellow",
        }
    return {
        "self_check": "ok",
        "status_lamp": "green",
    }


def _dedupe_preserve(seq: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _socket_segment(
    massive: Optional[Dict[str, Any]],
    ib_ingestor: Optional[Dict[str, Any]],
    ib_account_agent: Optional[Dict[str, Any]],
    quotes_redis_reader_ok: bool,
) -> tuple[int, List[str]]:
    """Socket / quotes path: Massive WS meta, IB ingestor, IB Account Agent, Monitor quotes Redis reader."""
    reasons: List[str] = []
    rank = 1
    if massive and massive.get("configured") and massive.get("ws_connected") is False:
        rank = max(rank, 2)
        reasons.append("socket_massive_disconnected")
    if ib_ingestor is not None and ib_ingestor.get("connected") is False:
        rank = max(rank, 2)
        reasons.append("socket_ib_ingestor_disconnected")
    if ib_account_agent is not None and ib_account_agent.get("connected") is False:
        rank = max(rank, 2)
        reasons.append("socket_ib_account_agent_disconnected")
    if not quotes_redis_reader_ok:
        rank = max(rank, 2)
        reasons.append("market_quotes_redis_unavailable")
    return rank, reasons


def _celery_segment(
    celery_broker_connected: bool,
    celery_workers: List[str],
) -> tuple[int, List[str]]:
    if not celery_broker_connected:
        return 2, ["celery_broker_down"]
    if not celery_workers:
        return 2, ["celery_no_workers"]
    return 1, []


def derive_health_roll_up(
    *,
    daemon_lamp: str,
    daemon_block_reasons: List[str],
    monitor_lamp: str,
    monitor_block_reasons: List[str],
    massive: Optional[Dict[str, Any]],
    ib_ingestor: Optional[Dict[str, Any]],
    quotes_redis_reader_ok: bool,
    celery_broker_connected: bool,
    celery_workers: List[str],
    ib_account_agent: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """System health: worst of daemon, socket/quotes, Celery, and Monitor lamps (with merged block_reasons)."""
    dr = _lamp_rank(daemon_lamp)
    mr = _lamp_rank(monitor_lamp)
    sr, socket_reasons = _socket_segment(
        massive, ib_ingestor, ib_account_agent, quotes_redis_reader_ok
    )
    cr, celery_reasons = _celery_segment(
        celery_broker_connected, celery_workers
    )
    worst = max(dr, mr, sr, cr)
    base = _rank_to_health(worst)

    br: List[str] = []
    if dr >= 2:
        br.extend(daemon_block_reasons or [])
    if sr >= 2:
        br.extend(socket_reasons)
    if cr >= 2:
        br.extend(celery_reasons)
    if mr >= 2:
        br.extend(monitor_block_reasons or [])

    return {
        "self_check": base["self_check"],
        "status_lamp": base["status_lamp"],
        "block_reasons": _dedupe_preserve(br),
    }
