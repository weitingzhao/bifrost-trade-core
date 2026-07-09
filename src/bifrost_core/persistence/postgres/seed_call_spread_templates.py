"""Idempotent seed for Bull Call Spread and Bear Call Spread strategy templates."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from bifrost_core.monitor.reader import template_config
from bifrost_core.monitor.reader import template_config_write

_DIM_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("direction", "dim_direction"),
    ("structure", "dim_structure"),
    ("coverage", "dim_coverage"),
    ("risk", "dim_risk"),
    ("volatility", "dim_volatility"),
    ("time", "dim_time"),
)

_CALL_SPREAD_TEMPLATE_SPECS: List[Dict[str, Any]] = [
    {
        "template_code": "bull_call_spread",
        "display_name": "Bull Call Spread",
        "dim_preferences": {
            "direction": ("bullish",),
            "structure": ("vertical",),
            "coverage": ("uncovered", "naked", "spread"),
            "risk": ("defined",),
            "volatility": ("long_vol", "neutral", "short_vol"),
            "time": ("monthly", "weekly"),
        },
        "explanation": "Debit vertical call spread: long lower-strike call, short higher-strike call.",
        "typical_use": "Moderately bullish outlook with limited capital and defined risk/reward.",
        "example": "Long 180C / Short 190C on the same expiry.",
        "nature": "Directional debit spread",
        "sort_order": 100,
        "characteristics": [
            "Bullish directional bias with capped upside above the short strike.",
            "Max loss limited to net debit paid at entry.",
            "Requires two call legs on the same underlying and expiration.",
        ],
        "legs": [
            {"role": "call", "direction": "long", "option_right": "C", "quantity": 1},
            {"role": "call", "direction": "short", "option_right": "C", "quantity": 1},
        ],
    },
    {
        "template_code": "bear_call_spread",
        "display_name": "Bear Call Spread",
        "dim_preferences": {
            "direction": ("bearish", "neutral"),
            "structure": ("vertical",),
            "coverage": ("uncovered", "naked", "spread"),
            "risk": ("defined",),
            "volatility": ("short_vol", "neutral"),
            "time": ("monthly", "weekly"),
        },
        "explanation": "Credit vertical call spread: short lower-strike call, long higher-strike call.",
        "typical_use": "Neutral to bearish outlook; collect premium with defined max loss.",
        "example": "Short 190C / Long 200C on the same expiry.",
        "nature": "Income credit spread",
        "sort_order": 101,
        "characteristics": [
            "Neutral to bearish bias; profits when price stays below the short strike.",
            "Max loss defined by spread width minus net credit received.",
            "Requires two call legs on the same underlying and expiration.",
        ],
        "legs": [
            {"role": "call", "direction": "short", "option_right": "C", "quantity": 1},
            {"role": "call", "direction": "long", "option_right": "C", "quantity": 1},
        ],
    },
]


def _pick_dim_code(conn: Any, dim_type: str, candidates: Sequence[str]) -> Optional[str]:
    for code in candidates:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM strategy_dim WHERE dim_type = %s AND code = %s",
                (dim_type, code),
            )
            if cur.fetchone():
                return code
    return None


def _build_template_payload(conn: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    prefs: Dict[str, Sequence[str]] = spec.get("dim_preferences") or {}
    payload: Dict[str, Any] = {
        "template_code": spec["template_code"],
        "display_name": spec["display_name"],
        "explanation": spec.get("explanation"),
        "typical_use": spec.get("typical_use"),
        "example": spec.get("example"),
        "nature": spec.get("nature"),
        "sort_order": spec.get("sort_order", 0),
        "is_active": True,
    }
    for dim_type, col in _DIM_FIELDS:
        code = _pick_dim_code(conn, dim_type, prefs.get(dim_type, ()))
        if code:
            payload[col] = code
    return payload


def seed_call_spread_templates(status_config: Optional[dict]) -> Dict[str, int]:
    """Ensure bull_call_spread and bear_call_spread templates exist with default legs.

    Returns mapping template_code -> strategy_template_id.
    """
    from bifrost_core.monitor.reader.template_config_write import _conn_from_config

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    out: Dict[str, int] = {}
    try:
        for spec in _CALL_SPREAD_TEMPLATE_SPECS:
            code = spec["template_code"]
            existing = template_config.get_template_by_code(conn, code)
            payload = _build_template_payload(conn, spec)
            if existing:
                tid = int(existing["strategy_template_id"])
                template_config_write.update_template(status_config, tid, payload)
            else:
                tid = template_config_write.create_template(status_config, payload)
            template_config_write.replace_template_legs(status_config, tid, spec["legs"])
            chars = spec.get("characteristics") or []
            if chars:
                template_config_write.replace_template_characteristics(status_config, tid, chars)
            out[code] = tid
        return out
    finally:
        conn.close()
