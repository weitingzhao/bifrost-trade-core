"""Derive Socket Services IB probe fields for GET /status (next check, stale flag)."""

from __future__ import annotations

from typing import Any, Dict


def attach_ib_probe_derived(
    target: Dict[str, Any],
    *,
    probe_at: float,
    probe_interval: float,
    probe_ok: bool,
    stale_mult: float,
    now: float,
    default_interval: float = 5.0,
) -> None:
    """Mutate ``target`` with last/next/stale probe fields when ``probe_at`` is from a live writer."""
    pa = float(probe_at or 0.0)
    if pa <= 0:
        return
    iv = float(probe_interval or 0.0)
    if iv <= 0:
        iv = float(default_interval)
    sm = float(stale_mult or 2.5)
    if sm < 1.0:
        sm = 1.0
    target["last_ib_probe_at"] = pa
    target["ib_probe_interval_sec"] = iv
    target["ib_probe_ok"] = bool(probe_ok)
    target["next_ib_probe_in_s"] = max(0.0, pa + iv - now)
    target["ib_probe_stale"] = (now - pa) > sm * iv


def parse_redis_probe_triple(
    h: Dict[str, Any],
    at_key: str,
    ok_key: str,
    interval_key: str,
) -> tuple[float, bool, float]:
    """Read probe triple from a Redis hash (string values)."""
    try:
        pa = float(h.get(at_key) or 0)
    except (TypeError, ValueError):
        pa = 0.0
    raw_ok = h.get(ok_key)
    if isinstance(raw_ok, bool):
        ok = raw_ok
    elif isinstance(raw_ok, (int, float)):
        ok = raw_ok != 0
    else:
        s = str(raw_ok or "").strip().lower()
        ok = s in ("1", "true", "yes", "on")
    try:
        iv = float(h.get(interval_key) or 0)
    except (TypeError, ValueError):
        iv = 0.0
    return pa, ok, iv


def attach_service_heartbeat_derived(
    target: Dict[str, Any],
    *,
    interval_sec: float,
    last_heartbeat_at: float,
    now: float,
) -> None:
    """Countdown to next main-thread service heartbeat (process alive + reconnect gate)."""
    iv = float(interval_sec or 0.0)
    if iv <= 0:
        return
    lh = float(last_heartbeat_at or 0.0)
    target["service_heartbeat_interval_sec"] = iv
    target["last_service_heartbeat_at"] = lh if lh > 0 else None
    if lh <= 0:
        target["next_service_heartbeat_in_s"] = iv
    else:
        target["next_service_heartbeat_in_s"] = max(0.0, lh + iv - now)
