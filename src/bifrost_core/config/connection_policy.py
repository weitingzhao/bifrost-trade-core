"""Shared IB reconnect backoff and probe interval defaults for Operator / Ingestor / Account Agent."""

from __future__ import annotations

from typing import Any, Dict

DEFAULT_RECONNECT_BASE_SEC = 2.0
DEFAULT_RECONNECT_MAX_SEC = 60.0
DEFAULT_IB_PROBE_INTERVAL_SEC = 5.0
DEFAULT_IB_PROBE_STALE_MULTIPLIER = 2.5
DEFAULT_RECONNECT_MAX_EXP = 6


def reconnect_delay_s(
    attempt_1_based: int,
    *,
    base: float = DEFAULT_RECONNECT_BASE_SEC,
    max_s: float = DEFAULT_RECONNECT_MAX_SEC,
    max_exp: int = DEFAULT_RECONNECT_MAX_EXP,
) -> float:
    """Exponential backoff matching scripts/systemd/run_ib_ingestor (cap exponent, cap max delay)."""
    if attempt_1_based < 1:
        attempt_1_based = 1
    exp = min(attempt_1_based - 1, max_exp)
    return min(base * (2**exp), max_s)


def get_ib_connection_policy(config: Dict[str, Any]) -> Dict[str, Any]:
    """Parse top-level ``ib_connection`` from merged YAML with defaults."""
    raw = config.get("ib_connection")
    if not isinstance(raw, dict):
        raw = {}

    def _f(key: str, default: float) -> float:
        v = raw.get(key)
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def _i(key: str, default: int) -> int:
        v = raw.get(key)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    base = _f("reconnect_base_sec", DEFAULT_RECONNECT_BASE_SEC)
    max_s = _f("reconnect_max_sec", DEFAULT_RECONNECT_MAX_SEC)
    probe = _f("ib_probe_interval_sec", DEFAULT_IB_PROBE_INTERVAL_SEC)
    stale = _f("ib_probe_stale_multiplier", DEFAULT_IB_PROBE_STALE_MULTIPLIER)
    max_exp = _i("reconnect_max_exp", DEFAULT_RECONNECT_MAX_EXP)

    if base < 0.5:
        base = 0.5
    if max_s < base:
        max_s = base
    if probe < 1.0:
        probe = 1.0
    if stale < 1.0:
        stale = 1.0
    if max_exp < 0:
        max_exp = 0

    return {
        "reconnect_base_sec": base,
        "reconnect_max_sec": max_s,
        "reconnect_max_exp": max_exp,
        "ib_probe_interval_sec": probe,
        "ib_probe_stale_multiplier": stale,
    }


def merge_ib_policy_into_effective_ib(out: Dict[str, Any], config: Dict[str, Any]) -> None:
    """Mutate ``out`` (get_effective_ib_config result) with flat ``ib_*_policy`` keys."""
    p = get_ib_connection_policy(config)
    out["ib_reconnect_base_sec"] = p["reconnect_base_sec"]
    out["ib_reconnect_max_sec"] = p["reconnect_max_sec"]
    out["ib_reconnect_max_exp"] = p["reconnect_max_exp"]
    out["ib_probe_interval_sec"] = p["ib_probe_interval_sec"]
    out["ib_probe_stale_multiplier"] = p["ib_probe_stale_multiplier"]


def operator_effective_health_refresh_sec(
    ib_operator_cfg: Dict[str, Any],
    probe_interval_sec: float,
) -> float:
    """Idle health loop interval: at least YAML health_refresh_sec and global probe interval."""
    hr = float((ib_operator_cfg or {}).get("health_refresh_sec") or 30)
    return max(hr, float(probe_interval_sec))
