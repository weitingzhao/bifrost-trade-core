"""Single source of truth for structure type config allowlists (param_kind, meta_key, meta_value_text).

Used by structure_type_config_write validation and by options API for Type Config UI.
Adding new allowed values requires changing this module and redeploying.

Display labels: stored value (DB/API) vs human-readable label (UI). Labels are optional;
if missing, the value is shown as-is.
"""

from typing import Any, Dict, List, Tuple

# Six orthogonal dimensions for strategy_template and strategy_structure.
DIM_TYPE_ALLOWED: Tuple[str, ...] = (
    "direction",
    "structure",
    "coverage",
    "risk",
    "volatility",
    "time",
)

DIM_TYPE_LABELS: Dict[str, str] = {
    "direction": "Direction",
    "structure": "Structure",
    "coverage": "Underlying coverage",
    "risk": "Risk profile",
    "volatility": "Volatility exposure",
    "time": "Time horizon",
}

# param_kind: Wizard uses "fixed" (no editable input) and "percent" (number 1-50). Optional future: "integer".
PARAM_KIND_ALLOWED: Tuple[str, ...] = ("fixed", "percent")

# Display label for each param_kind value (value -> label for UI).
PARAM_KIND_LABELS: Dict[str, str] = {
    "fixed": "Fixed (no input)",
    "percent": "Percent (%)",
}

# meta_key allowed per structure_type. Only keys listed here are used by Wizard/infer logic.
# Empty tuple = no meta config allowed for that type (avoids "config that has no effect").
ALLOWED_META_KEYS_BY_TYPE: Dict[str, Tuple[str, ...]] = {
    "covered_call": ("call_strike_rule", "otm_pct", "itm_pct"),
    # Other types: no downstream meta logic yet; allowlist empty so config has no effect until we add support.
}

# Display label for each meta_key (key -> label for UI). Shared across structure types.
META_KEY_LABELS: Dict[str, str] = {
    "call_strike_rule": "Call strike rule",
    "otm_pct": "OTM %",
    "itm_pct": "ITM %",
}

# meta_value_text allowed for (structure_type, meta_key). Used for infer rules and fixed default_value_text.
# If (type, key) not in dict, any non-empty string is allowed (e.g. numeric for otm_pct/itm_pct).
ALLOWED_META_VALUES_BY_TYPE_AND_KEY: Dict[Tuple[str, str], Tuple[str, ...]] = {
    # Covered Call call_strike_rule represents conceptual strike-rule buckets, not exact percentages.
    # Internal values intentionally avoid hard-coded numbers; specific % is controlled by otm_pct/itm_pct params.
    ("covered_call", "call_strike_rule"): ("normal_otm", "atm", "itm", "deep_otm"),
}

# Display label for meta_value_text per (structure_type, meta_key). value -> label for UI.
META_VALUE_LABELS_BY_TYPE_AND_KEY: Dict[Tuple[str, str], Dict[str, str]] = {
    ("covered_call", "call_strike_rule"): {
        "normal_otm": "Normal OTM",
        "atm": "ATM",
        "itm": "ITM",
        "deep_otm": "Deep OTM",
    },
}

# Default legs (strategy_structure_type_leg): role, direction, option_right allowlists for Type Config.
LEG_ROLE_ALLOWED: Tuple[str, ...] = ("underlying", "call", "put")
LEG_ROLE_LABELS: Dict[str, str] = {
    "underlying": "Underlying (stock)",
    "call": "Call",
    "put": "Put",
}

LEG_DIRECTION_ALLOWED: Tuple[str, ...] = ("long", "short")
LEG_DIRECTION_LABELS: Dict[str, str] = {
    "long": "Long",
    "short": "Short",
}

# option_right: empty string = stock leg; "C" = call, "P" = put.
LEG_OPTION_RIGHT_ALLOWED: Tuple[str, ...] = ("", "C", "P")
LEG_OPTION_RIGHT_LABELS: Dict[str, str] = {
    "": "— (stock)",
    "C": "Call",
    "P": "Put",
}


def get_param_kind_options() -> Tuple[str, ...]:
    return PARAM_KIND_ALLOWED


def get_meta_key_options(structure_type: str) -> Tuple[str, ...]:
    key = (structure_type or "").strip()
    return ALLOWED_META_KEYS_BY_TYPE.get(key, ())


def get_meta_value_options(structure_type: str, meta_key: str) -> Tuple[str, ...]:
    key = ((structure_type or "").strip(), (meta_key or "").strip())
    return ALLOWED_META_VALUES_BY_TYPE_AND_KEY.get(key, ())


def get_param_kind_options_with_labels() -> List[Dict[str, Any]]:
    """Return options as [{ value, label }] for Type Config UI. Label falls back to value if not in PARAM_KIND_LABELS."""
    return [
        {"value": v, "label": PARAM_KIND_LABELS.get(v, v)}
        for v in PARAM_KIND_ALLOWED
    ]


def get_meta_key_options_with_labels(structure_type: str) -> List[Dict[str, Any]]:
    """Return options as [{ value, label }] for Type Config UI. Label falls back to value if not in META_KEY_LABELS."""
    values = get_meta_key_options(structure_type)
    return [
        {"value": v, "label": META_KEY_LABELS.get(v, v)}
        for v in values
    ]


def get_meta_value_options_with_labels(structure_type: str, meta_key: str) -> List[Dict[str, Any]]:
    """Return options as [{ value, label }] for Type Config UI. Label falls back to value if not in labels dict."""
    values = get_meta_value_options(structure_type, meta_key)
    key = ((structure_type or "").strip(), (meta_key or "").strip())
    labels = META_VALUE_LABELS_BY_TYPE_AND_KEY.get(key, {})
    return [
        {"value": v, "label": labels.get(v, v)}
        for v in values
    ]


def get_leg_role_options_with_labels() -> List[Dict[str, Any]]:
    """Return leg role options as [{ value, label }] for Type Config Default legs UI."""
    return [
        {"value": v, "label": LEG_ROLE_LABELS.get(v, v)}
        for v in LEG_ROLE_ALLOWED
    ]


def get_leg_direction_options_with_labels() -> List[Dict[str, Any]]:
    """Return leg direction options as [{ value, label }] for Type Config Default legs UI."""
    return [
        {"value": v, "label": LEG_DIRECTION_LABELS.get(v, v)}
        for v in LEG_DIRECTION_ALLOWED
    ]


def get_leg_option_right_options_with_labels() -> List[Dict[str, Any]]:
    """Return leg option_right options as [{ value, label }]. Empty value = stock leg."""
    return [
        {"value": v, "label": LEG_OPTION_RIGHT_LABELS.get(v, v if v else "— (stock)")}
        for v in LEG_OPTION_RIGHT_ALLOWED
    ]
