"""Request bodies for /strategies/plans.

A plan is what the desk *intends*: the legs, the size, and how it means to get
out. It is a record for comparing against later, never an instruction -- no
daemon and no gateway reads this table (D10).
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

PriceEffect = Literal["credit", "debit"]
TargetKind = Literal["credit_pct", "option_price", "underlying_price"]
StopKind = Literal["credit_multiple", "option_price", "underlying_price"]
SourceKind = Literal["manual", "symbol", "hypothesis", "inbox_draft", "roll"]


class PlanLeg(BaseModel):
    """One leg of a planned structure, as it stood when the plan was written."""

    side: Literal["buy", "sell"]
    sec_type: Literal["OPT", "STK"]
    right: Optional[Literal["C", "P"]] = None
    strike: Optional[float] = None
    expiry: Optional[str] = Field(None, description="YYYY-MM-DD")
    ratio: int = Field(1, ge=1)
    contract_key: Optional[str] = Field(None, description="SYM|OPT|YYYYMMDD|STRIKE|R")
    mid_at_plan: Optional[float] = Field(
        None, description="Mid when the plan was written; null when no quote was taken"
    )
    quote_asof: Optional[str] = Field(None, description="ISO 8601 timestamp of mid_at_plan")


class PlanCreateBody(BaseModel):
    """Create one plan. It starts as a draft; `intend` is a separate step."""

    account_id: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    structure_label: str = Field(..., min_length=1)
    strategy_structure_id: Optional[int] = None
    strategy_opportunity_id: Optional[int] = None
    legs: List[PlanLeg] = Field(default_factory=list)
    qty: int = Field(..., gt=0)
    price_effect: Optional[PriceEffect] = None
    limit_price: Optional[float] = Field(None, ge=0)
    target_kind: Optional[TargetKind] = None
    target_value: Optional[float] = None
    stop_kind: Optional[StopKind] = None
    stop_value: Optional[float] = None
    exit_by: Optional[str] = Field(None, description="YYYY-MM-DD, the latest planned exit")
    rationale: Optional[str] = None
    source_kind: SourceKind = "manual"
    source_ref: Optional[str] = None
    source: List[Dict[str, Any]] = Field(
        default_factory=list, description="Provenance chain: [{kind, text, ref?, to?}]"
    )
    expires_at: Optional[str] = Field(None, description="ISO 8601; after this an intent reads expired")
    parent_strategy_plan_id: Optional[int] = Field(
        None, description="The plan this one rolls; set with source_kind='roll'"
    )


class PlanUpdateBody(BaseModel):
    """Edit a draft. A plan that has been marked intended is no longer editable."""

    account_id: Optional[str] = Field(None, min_length=1)
    symbol: Optional[str] = Field(None, min_length=1)
    structure_label: Optional[str] = Field(None, min_length=1)
    strategy_structure_id: Optional[int] = None
    strategy_opportunity_id: Optional[int] = None
    legs: Optional[List[PlanLeg]] = None
    qty: Optional[int] = Field(None, gt=0)
    price_effect: Optional[PriceEffect] = None
    limit_price: Optional[float] = Field(None, ge=0)
    target_kind: Optional[TargetKind] = None
    target_value: Optional[float] = None
    stop_kind: Optional[StopKind] = None
    stop_value: Optional[float] = None
    exit_by: Optional[str] = None
    rationale: Optional[str] = None
    source_kind: Optional[SourceKind] = None
    source_ref: Optional[str] = None
    source: Optional[List[Dict[str, Any]]] = None
    expires_at: Optional[str] = None


class PlanLinkFillBody(BaseModel):
    """Say which open instance this plan turned into. The fill happened in TWS."""

    strategy_instance_id: int
