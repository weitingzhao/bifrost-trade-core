"""Request bodies for /strategies/* (Phase A management API)."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, get_args

from pydantic import BaseModel, Field

# strategy_opportunity.scope_type (TD-71, core 0.41.0): where the symbols came from. `symbols`
# is what the rule covers either way; `watchlist_stk` needs at least one. NULL = not said.
# The table's CHECK strategy_opportunity_scope_type_ck holds the same set.
ScopeType = Literal["watchlist_stk", "explicit_symbols"]
SCOPE_TYPES: tuple[str, ...] = get_args(ScopeType)


class EntryConditionBody(BaseModel):
    """One entry condition row for opportunity create/update."""

    condition_type: str = Field(
        ...,
        description="e.g. iv_min, iv_max, dte_min, dte_max, earnings_blackout_days, min_volume",
    )
    value_text: Optional[str] = None
    value_numeric: Optional[float] = None


class OpportunityBody(BaseModel):
    """Request body for create strategy opportunity (scope_type + symbols + entry_conditions, no jsonb)."""

    name: str = Field(..., min_length=1)
    strategy_structure_id: int
    default_gate_safety_strategy_id: Optional[int] = None
    scope_type: Optional[Literal["watchlist_stk", "explicit_symbols", ""]] = Field(
        None,
        description="watchlist_stk | explicit_symbols ('' or null = not said): where the symbols came from",
    )
    symbols: Optional[List[str]] = Field(
        default_factory=list,
        description="The symbols the rule covers (whatever scope_type says); watchlist_stk needs at least one",
    )
    entry_conditions: Optional[List[EntryConditionBody]] = Field(default_factory=list)
    is_active: bool = True


class OpportunityUpdateBody(BaseModel):
    """Request body for update strategy opportunity; all fields optional for partial update."""

    name: Optional[str] = Field(None, min_length=1)
    strategy_structure_id: Optional[int] = None
    default_gate_safety_strategy_id: Optional[int] = None
    scope_type: Optional[Literal["watchlist_stk", "explicit_symbols", ""]] = None
    symbols: Optional[List[str]] = None
    entry_conditions: Optional[List[EntryConditionBody]] = None
    is_active: Optional[bool] = None


class AllocationBody(BaseModel):
    """Request body for create strategy allocation."""

    name: str = Field(..., min_length=1)
    strategy_opportunity_ids: List[int] = Field(..., description="List of strategy_opportunity_id")
    gate_safety_strategy_id: Optional[int] = None
    allocation_limits: Optional[Dict[str, Any]] = Field(None, description="e.g. max_positions, max_bp_pct")
    is_active: bool = True


class AllocationUpdateBody(BaseModel):
    """Request body for update strategy allocation; all fields optional for partial update."""

    name: Optional[str] = Field(None, min_length=1)
    strategy_opportunity_ids: Optional[List[int]] = None
    gate_safety_strategy_id: Optional[int] = None
    allocation_limits: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class StrategyInstanceCreateBody(BaseModel):
    """Request body for create strategy instance (SI.2)."""

    strategy_opportunity_id: int = Field(..., description="Parent opportunity ID")
    account_id: str = Field(..., min_length=1, description="Account ID")
    opened_at: str = Field(..., description="Opened at (ISO 8601 or Unix timestamp string)")
    label: Optional[str] = Field(None, description="Optional label")
    notes: Optional[str] = Field(None, description="Optional notes")


class StrategyInstanceUpdateBody(BaseModel):
    """Request body for PATCH strategy instance; label, notes, created_at, opened_at optional."""

    label: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    opened_at: Optional[str] = None
