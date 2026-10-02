"""Pydantic models for gate_safety_strategy.params_json (Wave 9).

params_json stores a gate's earnings dates at strategy.earnings.dates, which is where
the daemon's config['gates'] reads them. Over the API they travel once, as the gate
row's top-level `earnings_dates`: the `gates` object a gate row carries, and the one
default_gates() returns, has no `dates` key (split_earnings_dates).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class GateStructureParams(BaseModel):
    min_dte: int = 21
    max_dte: int = 35
    atm_band_pct: float = 0.03


class GateEarningsParams(BaseModel):
    blackout_days_before: int = 3
    blackout_days_after: int = 1
    dates: List[str] = Field(default_factory=list)


class GateStrategyParams(BaseModel):
    structure: GateStructureParams = Field(default_factory=GateStructureParams)
    earnings: GateEarningsParams = Field(default_factory=GateEarningsParams)
    trading_hours_only: bool = True


class GateDeltaParams(BaseModel):
    epsilon_band: int = 10
    threshold_hedge_shares: int = 25
    max_delta_limit: int = 500


class GateMarketParams(BaseModel):
    vol_window_min: int = 5
    stale_ts_threshold_ms: int = 5000


class GateLiquidityParams(BaseModel):
    wide_spread_pct: float = 0.1
    extreme_spread_pct: float = 0.5


class GateSystemParams(BaseModel):
    data_lag_threshold_ms: int = 1000


class GateStateParams(BaseModel):
    delta: GateDeltaParams = Field(default_factory=GateDeltaParams)
    market: GateMarketParams = Field(default_factory=GateMarketParams)
    liquidity: GateLiquidityParams = Field(default_factory=GateLiquidityParams)
    system: GateSystemParams = Field(default_factory=GateSystemParams)


class GateHedgeParams(BaseModel):
    min_hedge_shares: int = 10
    cooldown_seconds: int = 60
    max_hedge_shares_per_order: int = 500
    min_price_move_pct: float = 0.2


class GateIntentParams(BaseModel):
    hedge: GateHedgeParams = Field(default_factory=GateHedgeParams)


class GateRiskParams(BaseModel):
    max_daily_hedge_count: int = 50
    max_position_shares: int = 2000
    max_daily_loss_usd: float = 5000.0
    max_net_delta_shares: int = 100
    max_spread_pct: float = 0.05
    paper_trade: bool = True


class GateGuardParams(BaseModel):
    risk: GateRiskParams = Field(default_factory=GateRiskParams)


class GateParams(BaseModel):
    """Nested config['gates'] shape stored in gate_safety_strategy.params_json."""

    strategy: GateStrategyParams = Field(default_factory=GateStrategyParams)
    state: GateStateParams = Field(default_factory=GateStateParams)
    intent: GateIntentParams = Field(default_factory=GateIntentParams)
    guard: GateGuardParams = Field(default_factory=GateGuardParams)


def split_earnings_dates(gates: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Return (a copy of gates without strategy.earnings.dates, those dates as strings)."""
    out = copy.deepcopy(gates)
    earnings = (out.get("strategy") or {}).get("earnings")
    raw = earnings.pop("dates", None) if isinstance(earnings, dict) else None
    dates = [str(d) for d in (raw or []) if d]
    return out, dates


def default_gates() -> Dict[str, Any]:
    """Default GateParams in the shape of a gate row's `gates` object, without earnings dates.

    JSON-serializable. Served by the API so a new gate starts from these values
    rather than a copy the frontend keeps.
    """
    gates, _ = split_earnings_dates(GateParams().model_dump(mode="json"))
    return gates


class TemplateLeg(BaseModel):
    role: Optional[str] = None
    direction: Optional[str] = None
    option_right: Optional[str] = None
    quantity: int = 1
    quantity_default: Optional[int] = None
    strike: Optional[float] = None
    expiration: Optional[str] = None


class StructureLeg(BaseModel):
    role: Optional[str] = None
    direction: Optional[str] = None
    option_right: Optional[str] = None
    quantity: int = 1
    strike: Optional[float] = None
    expiration: Optional[str] = None
