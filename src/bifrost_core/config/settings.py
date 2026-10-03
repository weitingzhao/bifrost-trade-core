"""Unified config: gates (strategy, state, intent, guard) for hedge logic and ExecutionGuard.

Option 2 (gates): pipeline-aligned structure. Backward compat: top-level and state_space.
Defaults: ``GateParams`` (``monitor.schemas.gate_params``), the same model that validates a
gate row. They used to come from a file named ``config.yaml.example`` found beside
``BIFROST_CONFIG`` — mounted only on the daemon, so renaming a file called "example" would have
crash-looped it (debt TD-53). The values are identical; the dependency on the file is gone.
"""

import logging
from typing import Any, Dict, Optional

from bifrost_core.core.dict_merge import deep_merge

logger = logging.getLogger(__name__)

_GATE_DEFAULTS: Optional[Dict[str, Any]] = None


def _gate_defaults() -> Dict[str, Any]:
    """Gate defaults in the runtime config's shape: ``{"gates": {...}}``."""
    global _GATE_DEFAULTS
    if _GATE_DEFAULTS is None:
        from bifrost_core.monitor.schemas.gate_params import GateParams

        _GATE_DEFAULTS = {"gates": GateParams().model_dump(mode="json")}
    return _GATE_DEFAULTS


def _merged_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The runtime config over the gate defaults: a key the config sets wins."""
    return deep_merge(_gate_defaults(), cfg)


def _gates_section(cfg: Dict[str, Any], gate: str, section: str) -> Dict[str, Any]:
    """Get section from gates.gate.section. Returns {} if not found."""
    g = cfg.get("gates") or {}
    gate_cfg = g.get(gate) or {}
    return gate_cfg.get(section) if isinstance(gate_cfg.get(section), dict) else {}


def _section(cfg: Dict[str, Any], section: str) -> Dict[str, Any]:
    """Get section, merging gates → top-level → state_space (later overrides earlier)."""
    result: Dict[str, Any] = {}
    if section in ("delta", "market", "liquidity", "system"):
        result = dict(_gates_section(cfg, "state", section) or {})
    elif section == "hedge":
        result = dict(_gates_section(cfg, "intent", "hedge") or {})
    elif section == "risk":
        result = dict(_gates_section(cfg, "guard", "risk") or {})
    elif section == "earnings":
        result = dict(_gates_section(cfg, "strategy", "earnings") or {})
    elif section == "structure":
        result = dict(_gates_section(cfg, "strategy", "structure") or {})

    top = cfg.get(section) or (cfg.get("state_space") or {}).get(section) or {}
    return {**result, **top}


def get_structure_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return structure config (min_dte, max_dte, atm_band_pct). From gates.strategy.structure or top-level."""
    cfg = config or {}
    merged = _merged_config(cfg)
    s = _section(merged, "structure")
    return {
        "min_dte": s.get("min_dte"),
        "max_dte": s.get("max_dte"),
        "atm_band_pct": s.get("atm_band_pct"),
    }


def get_risk_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return risk config (paper_trade, etc.). From gates.guard.risk or top-level risk."""
    cfg = config or {}
    merged = _merged_config(cfg)
    return _section(merged, "risk")


def get_hedge_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Return unified hedge + guard config from config.yaml.

    Reads from gates (strategy, state, intent, guard); fallback: top-level, state_space.
    Missing values from the GateParams defaults. Returns flat dict for GsTrading, ExecutionGuard.
    """
    cfg = config or {}
    merged = _merged_config(cfg)
    delta = _section(merged, "delta")
    hedge = _section(merged, "hedge")
    risk = _section(merged, "risk")
    earnings = _section(merged, "earnings")

    strategy = (merged.get("gates") or {}).get("strategy") or {}
    trading_hours = strategy.get("trading_hours_only")
    if trading_hours is None:
        trading_hours = risk.get("trading_hours_only")

    cooldown = hedge.get("cooldown_seconds")
    cooldown = int(cooldown) if cooldown is not None else None

    # Prefer legacy hedge_threshold when user provided it (backward compat)
    thresh = delta.get("hedge_threshold") or delta.get("threshold_hedge_shares")

    return {
        "threshold_hedge_shares": thresh,
        "min_hedge_shares": hedge.get("min_hedge_shares"),
        "cooldown_sec": cooldown,
        "max_hedge_shares_per_order": hedge.get("max_hedge_shares_per_order"),
        "min_price_move_pct": hedge.get("min_price_move_pct"),
        "max_daily_hedge_count": risk.get("max_daily_hedge_count"),
        "max_position_shares": risk.get("max_position_shares"),
        "max_daily_loss_usd": risk.get("max_daily_loss_usd"),
        "max_net_delta_shares": risk.get("max_net_delta_shares"),
        "max_spread_pct": risk.get("max_spread_pct"),
        "trading_hours_only": trading_hours,
        "earnings_dates": [d for d in (earnings.get("dates") or []) if d],
        "blackout_days_before": earnings.get("blackout_days_before"),
        "blackout_days_after": earnings.get("blackout_days_after"),
    }


def get_state_space_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return state space config. Sections: delta, market, liquidity, system, hedge.
    Reads from gates.state, gates.intent; missing values from the GateParams defaults."""
    cfg = config or {}
    merged = _merged_config(cfg)
    out: Dict[str, Any] = {}
    for section in ["delta", "market", "liquidity", "system", "hedge"]:
        out[section] = _section(merged, section)
    return out


def get_config_for_guards(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return config dict suitable for TradingGuard and StateClassifier."""
    cfg = config or {}
    state_cfg = get_state_space_config(cfg)
    merged = _merged_config(cfg)
    state_cfg["risk"] = _section(merged, "risk")
    return state_cfg
