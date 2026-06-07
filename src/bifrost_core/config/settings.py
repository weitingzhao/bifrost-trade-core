"""Unified config: gates (strategy, state, intent, guard) for hedge logic and ExecutionGuard.

Option 2 (gates): pipeline-aligned structure. Backward compat: top-level and state_space.
Defaults: loaded from config/config.yaml.example (single source of truth, no code-level defaults).
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

from bifrost_core.core.dict_merge import deep_merge

# Lazy-loaded example config (single source of truth for defaults)
_EXAMPLE_CONFIG: Optional[Dict[str, Any]] = None


def _config_defaults_dir() -> Path:
    """Directory holding config.yaml / config.yaml.example (Docker: /app/config)."""
    env_cfg = (os.environ.get("BIFROST_CONFIG") or "").strip()
    if env_cfg:
        return Path(env_cfg).resolve().parent
    return Path(__file__).resolve().parents[3] / "config"


def _load_example_config() -> Dict[str, Any]:
    """Load config.yaml.example as defaults. No code-level defaults."""
    global _EXAMPLE_CONFIG
    if _EXAMPLE_CONFIG is None:
        cfg_dir = _config_defaults_dir()
        example_path = cfg_dir / "config.yaml.example"
        fallback_path = cfg_dir / "config.yaml"
        path = example_path if example_path.is_file() else fallback_path
        if not path.is_file():
            raise FileNotFoundError(
                f"Missing {example_path} (and no {fallback_path} to fall back). "
                "Restore the tracked template config/config.yaml.example from the repository."
            )
        if path == fallback_path:
            logger.warning(
                "config/config.yaml.example missing; using %s as merge base for gate defaults. "
                "Add config.yaml.example for consistent defaults across machines.",
                fallback_path,
            )
        with open(path, encoding="utf-8") as f:
            _EXAMPLE_CONFIG = yaml.safe_load(f) or {}
    return _EXAMPLE_CONFIG


def _merged_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Merge config with example so missing keys come from config file."""
    return deep_merge(_load_example_config(), cfg)


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
    Missing values from config/config.yaml.example. Returns flat dict for GsTrading, ExecutionGuard.
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
    Reads from gates.state, gates.intent; missing values from config.yaml.example."""
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
