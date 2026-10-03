"""Pydantic models for gate_safety_strategy.params_json (Wave 9), and the leg models (TD-44).

Two different jsonb columns are both called ``params_json``: here, a gate's
``gate_safety_strategy.params_json`` is one GateParams *object*; a template's
``strategy_template.params_json`` is an *array of parameter definitions*
(``{meta_key, display_label, param_kind, default_value_text, sort_order}``), served by the
API as ``meta_params``.

params_json stores a gate's earnings dates at strategy.earnings.dates, which is where
the daemon's config['gates'] reads them. Over the API they travel once, as the gate
row's top-level `earnings_dates`: the `gates` object a gate row carries, and the one
default_gates() returns, has no `dates` key (split_earnings_dates).
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

from bifrost_core.portfolio.contract_key import opt_key


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


# --- legs (TD-44, core 0.41.0) ------------------------------------------------------------
#
# Two kinds of leg, not three copies of one:
#
# * AbstractLeg -- a slot of a template (strategy_template.legs_json) or a structure
#   (strategy_structure.legs_json): a direction and a right, no contract.
# * PlanLeg (schemas/strategy_plans.py) -- a concrete contract of a plan (strategy_plan.legs_json):
#   side, sec_type, right, strike, expiry, ratio.
#
# abstract_leg_to_plan_leg is the one mapping between them (long -> buy, short -> sell,
# option_right -> right, quantity -> ratio). No column is renamed (Owner 2026-10-03).

LegRole = Literal["underlying", "call", "put"]
LegDirection = Literal["long", "short"]
# "" is a stock leg, as the structure form writes it.
LegOptionRight = Literal["", "C", "P"]


class AbstractLeg(BaseModel):
    """One slot of a template or a structure. Writers of either jsonb go through it."""

    role: Optional[LegRole] = None
    direction: Optional[LegDirection] = None
    option_right: Optional[LegOptionRight] = None
    quantity: int = Field(1, ge=1)
    # Templates only: the quantity a new structure starts with.
    quantity_default: Optional[int] = Field(None, ge=1)
    sort_order: Optional[int] = None
    # Deprecated (structure legs): never filled on any env (read 2026-10-03); kept because the
    # structure form still reads them for display. Do not write a contract here -- that is a plan.
    strike: Optional[float] = None
    expiration: Optional[str] = None


# The names the writers used before 0.41.0; one model now.
TemplateLeg = AbstractLeg
StructureLeg = AbstractLeg

_DIRECTION_TO_SIDE = {"long": "buy", "short": "sell"}


def abstract_leg_to_plan_leg(
    leg: Any,
    *,
    symbol: str,
    expiry: Optional[str] = None,
    strike: Optional[float] = None,
) -> Dict[str, Any]:
    """A concrete plan leg (the ``strategy_plan.legs_json`` shape) from a template / structure slot.

    ``expiry`` is ``YYYY-MM-DD``; an option slot (option_right C/P) needs it and ``strike``,
    a stock slot (``""`` / None) takes neither. ``contract_key`` is the positions format
    (``SYM|OPT|YYYYMMDD|80.0|C``) for an option, None for stock. Raises ValueError when the
    slot has no direction or an option slot lacks expiry / strike.
    """
    slot = leg if isinstance(leg, AbstractLeg) else AbstractLeg.model_validate(leg)
    if slot.direction is None:
        raise ValueError("leg has no direction (long / short)")
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    right = slot.option_right or None
    if right is None:
        return {
            "side": _DIRECTION_TO_SIDE[slot.direction],
            "sec_type": "STK",
            "right": None,
            "strike": None,
            "expiry": None,
            "ratio": slot.quantity,
            "contract_key": None,
            "mid_at_plan": None,
            "quote_asof": None,
        }
    if not expiry or strike is None:
        raise ValueError("an option leg needs expiry and strike")
    exp = str(expiry).strip()
    try:
        datetime.strptime(exp, "%Y-%m-%d")
    except ValueError:
        raise ValueError("expiry must be YYYY-MM-DD") from None
    k = float(strike)
    return {
        "side": _DIRECTION_TO_SIDE[slot.direction],
        "sec_type": "OPT",
        "right": right,
        "strike": k,
        "expiry": exp,
        "ratio": slot.quantity,
        "contract_key": opt_key(sym, exp.replace("-", ""), k, right),
        "mid_at_plan": None,
        "quote_asof": None,
    }
