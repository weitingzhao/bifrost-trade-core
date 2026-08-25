"""Read-only strategy dimension catalog (Wave 9 — replaces strategy_dim table)."""

from __future__ import annotations

from typing import Any, Dict, List

from bifrost_core.monitor.reader import structure_type_config_constants as _const

# Canonical dimension values (enum literals). Labels are UI-facing English strings.
_DIM_ENTRIES: Dict[str, List[tuple[str, str, int]]] = {
    "direction": [
        ("bullish", "Bullish", 0),
        ("bearish", "Bearish", 10),
        ("neutral", "Neutral", 20),
        ("long", "Long", 30),
        ("short", "Short", 40),
    ],
    "structure": [
        ("vertical", "Vertical", 0),
        ("calendar", "Calendar", 10),
        ("diagonal", "Diagonal", 20),
        ("covered_call", "Covered call", 30),
        ("income", "Income", 40),
        ("combo", "Combo", 50),
    ],
    "coverage": [
        ("uncovered", "Uncovered", 0),
        ("naked", "Naked", 10),
        ("spread", "Spread", 20),
        ("covered", "Covered", 30),
        ("partial", "Partial", 40),
    ],
    "risk": [
        ("defined", "Defined risk", 0),
        ("undefined", "Undefined risk", 10),
        ("limited", "Limited risk", 20),
    ],
    "volatility": [
        ("long_vol", "Long volatility", 0),
        ("short_vol", "Short volatility", 10),
        ("neutral", "Neutral volatility", 20),
    ],
    "time": [
        ("weekly", "Weekly", 0),
        ("monthly", "Monthly", 10),
        ("quarterly", "Quarterly", 20),
        ("leap", "LEAP", 30),
    ],
}

_DIM_ID_COUNTER = 1
_DIM_BY_TYPE: Dict[str, List[Dict[str, Any]]] = {}
_ALLOWED_CODES: Dict[str, set[str]] = {}

for dim_type, entries in _DIM_ENTRIES.items():
    items: List[Dict[str, Any]] = []
    codes: set[str] = set()
    for code, label, sort_order in entries:
        items.append(
            {
                "strategy_dim_id": _DIM_ID_COUNTER,
                "dim_type": dim_type,
                "code": code,
                "display_label": label,
                "sort_order": sort_order,
            }
        )
        _DIM_ID_COUNTER += 1
        codes.add(code)
    _DIM_BY_TYPE[dim_type] = items
    _ALLOWED_CODES[dim_type] = codes

# Wave 10: canonical enum type names + literals for PostgreSQL dim_*_t types.
DIM_TYPE_TO_ENUM: Dict[str, str] = {
    "direction": "dim_direction_t",
    "structure": "dim_structure_t",
    "coverage": "dim_coverage_t",
    "risk": "dim_risk_t",
    "volatility": "dim_volatility_t",
    "time": "dim_time_t",
}


def dim_literals_by_type() -> Dict[str, tuple[str, ...]]:
    """Return dim codes per type (order matches catalog sort_order)."""
    return {
        dim_type: tuple(code for code, _, _ in entries) for dim_type, entries in _DIM_ENTRIES.items()
    }


def dim_default_code(dim_type: str) -> str:
    literals = dim_literals_by_type().get(dim_type, ())
    return literals[0] if literals else ""


def list_dims_grouped() -> Dict[str, List[Dict[str, Any]]]:
    return {dt: list(items) for dt, items in _DIM_BY_TYPE.items()}


def list_dims_by_type(dim_type: str) -> List[Dict[str, Any]]:
    key = (dim_type or "").strip()
    return list(_DIM_BY_TYPE.get(key, []))


def is_valid_dim_code(dim_type: str, code: str) -> bool:
    dt = (dim_type or "").strip()
    c = (code or "").strip()
    if not dt or not c:
        return False
    if dt not in _const.DIM_TYPE_ALLOWED:
        return False
    return c in _ALLOWED_CODES.get(dt, set())


def get_dim_by_id(strategy_dim_id: int) -> Dict[str, Any] | None:
    for items in _DIM_BY_TYPE.values():
        for item in items:
            if item["strategy_dim_id"] == strategy_dim_id:
                return dict(item)
    return None
