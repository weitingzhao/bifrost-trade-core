"""Leg schemas for strategy_structure writes.

A structure's schema comes from its template's legs (strategy_template.legs_json):
build_schema_from_legs turns them into the expected shape and validate_legs checks a
structure's legs against it. Used by strategy_structure_write.
"""

from typing import Any, Dict, List, Optional


def build_schema_from_legs(legs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build a schema dict (leg_count, legs with locked flags) from leg dicts (role/direction/option_right)."""
    if not isinstance(legs, list):
        return None
    if not legs:
        return {"leg_count": 0, "legs": []}
    return {
        "leg_count": len(legs),
        "legs": [
            {
                "role": leg.get("role"),
                "direction": leg.get("direction"),
                "option_right": leg.get("option_right"),
                "locked": True,
            }
            for leg in legs
        ],
    }


def validate_legs(structure_type: str, legs: List[Any], schema: Dict[str, Any]) -> None:
    """Validate legs against a schema from build_schema_from_legs.

    structure_type only names the structure in error messages. Raises ValueError if invalid.
    """
    expected_legs = schema.get("legs") or []
    ctx = structure_type or "structure"
    if not isinstance(legs, list):
        raise ValueError("legs must be an array")
    if len(legs) != len(expected_legs):
        raise ValueError(
            f"structure_type {ctx} requires exactly {len(expected_legs)} leg(s), got {len(legs)}"
        )
    for i, (exp, got) in enumerate(zip(expected_legs, legs)):
        if not isinstance(got, dict):
            raise ValueError(f"leg {i} must be an object")
        for field in ("role", "direction", "option_right"):
            exp_val = exp.get(field)
            got_val = got.get(field)
            if exp_val is None:
                if got_val in (None, ""):
                    continue
                raise ValueError(f"leg {i}: {field} must be empty for {ctx} (stock leg), got {got_val!r}")
            got_norm = (str(got_val).strip() if got_val is not None else "").upper()
            exp_norm = (str(exp_val).strip()).upper()
            if got_norm != exp_norm:
                raise ValueError(
                    f"leg {i}: {field} must be {exp_val!r} for {ctx}, got {got_val!r}"
                )
