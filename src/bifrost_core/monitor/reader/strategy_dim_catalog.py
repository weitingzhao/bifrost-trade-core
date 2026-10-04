"""Read-only strategy dimension catalog (Wave 9 — replaces strategy_dim table)."""

from __future__ import annotations

from typing import Any, Dict, List

from bifrost_core.monitor.reader import structure_type_config_constants as _const

# Canonical dimension values (enum literals). Labels are UI-facing English strings.
#
# These are the labels the dim_*_t types carry in bifrost_{dev,stg,prod}: the 25
# rows of the retired strategy_dim table (codes, labels and sort order as it held
# them), which the Wave 9 migration turned into the enums. ensure_dim_enum_types
# only creates a missing type and never alters one, so this list follows the
# databases, not the other way round. Adding a code takes both halves in one
# change: ALTER TYPE ... ADD VALUE in every env, and the entry here.
_DIM_ENTRIES: Dict[str, List[tuple[str, str, int]]] = {
    "direction": [
        ("bullish", "Bullish", 0),
        ("bearish", "Bearish", 1),
        ("neutral", "Neutral", 2),
    ],
    "structure": [
        ("single_leg", "Single leg", 0),
        ("vertical", "Vertical spread", 1),
        ("calendar", "Calendar spread", 2),
        ("diagonal", "Diagonal spread", 3),
        ("straddle", "Straddle / strangle", 4),
        ("condor", "Condor", 5),
        ("butterfly", "Butterfly", 6),
        ("ratio", "Ratio spread", 7),
        ("custom", "Custom", 8),
    ],
    "coverage": [
        ("covered", "Covered", 0),
        ("naked", "Naked", 1),
        ("cash_secured", "Cash secured", 2),
        ("synthetic", "Synthetic", 3),
    ],
    "risk": [
        ("defined", "Defined risk", 0),
        ("undefined", "Undefined risk", 1),
    ],
    "volatility": [
        ("long_vol", "Long volatility", 0),
        ("short_vol", "Short volatility", 1),
        ("vol_neutral", "Volatility neutral", 2),
    ],
    "time": [
        ("weekly", "Weekly", 0),
        ("monthly", "Monthly", 1),
        ("leaps", "LEAPS", 2),
        ("flex", "Flex DTE", 3),
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


def list_dims_grouped() -> Dict[str, List[Dict[str, Any]]]:
    return {dt: list(items) for dt, items in _DIM_BY_TYPE.items()}


def is_valid_dim_code(dim_type: str, code: str) -> bool:
    dt = (dim_type or "").strip()
    c = (code or "").strip()
    if not dt or not c:
        return False
    if dt not in _const.DIM_TYPE_ALLOWED:
        return False
    return c in _ALLOWED_CODES.get(dt, set())


def validate_dim_fields(payload: Dict[str, Any]) -> None:
    """Raise ValueError for a dim_* field whose code the catalog (and so the enum type) lacks.

    Blank and missing fields are allowed: every dim column is nullable.
    """
    for dim_type in DIM_TYPE_TO_ENUM:
        code = payload.get(f"dim_{dim_type}")
        if code is None or str(code).strip() == "":
            continue
        c = str(code).strip()
        if not is_valid_dim_code(dim_type, c):
            raise ValueError(f"Invalid {dim_type} code: {c}")
