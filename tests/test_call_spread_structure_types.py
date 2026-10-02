"""Bull/bear call spread leg schemas, built from template legs the way strategy_structure_write does."""

import pytest

from bifrost_core.monitor.reader import structure_type_schema

# Template legs as strategy_template.legs_json holds them (invented, not read from any env).
_BULL_CALL_SPREAD = [
    {"role": "call", "direction": "long", "option_right": "C", "quantity": 1},
    {"role": "call", "direction": "short", "option_right": "C", "quantity": 1},
]
_BEAR_CALL_SPREAD = [
    {"role": "call", "direction": "short", "option_right": "C", "quantity": 1},
    {"role": "call", "direction": "long", "option_right": "C", "quantity": 1},
]
_COVERED_CALL = [
    {"role": "underlying", "direction": "long", "option_right": None, "quantity": 1},
    {"role": "call", "direction": "short", "option_right": "C", "quantity": 1},
]


@pytest.mark.parametrize(
    ("template_legs", "long_idx", "short_idx"),
    [
        (_BULL_CALL_SPREAD, 0, 1),
        (_BEAR_CALL_SPREAD, 1, 0),
    ],
    ids=["bull_call_spread", "bear_call_spread"],
)
def test_call_spread_schema_from_legs(template_legs, long_idx: int, short_idx: int) -> None:
    schema = structure_type_schema.build_schema_from_legs(template_legs)
    assert schema is not None
    assert schema["leg_count"] == 2
    legs = schema["legs"]
    assert legs[long_idx] == {"role": "call", "direction": "long", "option_right": "C", "locked": True}
    assert legs[short_idx] == {"role": "call", "direction": "short", "option_right": "C", "locked": True}


def test_schema_from_no_legs_and_from_a_non_list() -> None:
    assert structure_type_schema.build_schema_from_legs([]) == {"leg_count": 0, "legs": []}
    assert structure_type_schema.build_schema_from_legs(None) is None  # type: ignore[arg-type]


def test_call_spread_validate_legs() -> None:
    schema = structure_type_schema.build_schema_from_legs(_BULL_CALL_SPREAD)
    structure_type_schema.validate_legs("bull_call_spread", _BULL_CALL_SPREAD, schema=schema)

    bad = [dict(_BULL_CALL_SPREAD[0]), dict(_BULL_CALL_SPREAD[1])]
    bad[1]["direction"] = "long"
    with pytest.raises(ValueError, match="direction"):
        structure_type_schema.validate_legs("bull_call_spread", bad, schema=schema)


def test_validate_legs_counts_legs() -> None:
    schema = structure_type_schema.build_schema_from_legs(_BULL_CALL_SPREAD)
    with pytest.raises(ValueError, match="exactly 2 leg"):
        structure_type_schema.validate_legs("bull_call_spread", _BULL_CALL_SPREAD[:1], schema=schema)


def test_validate_legs_stock_leg_takes_no_option_right() -> None:
    schema = structure_type_schema.build_schema_from_legs(_COVERED_CALL)
    structure_type_schema.validate_legs("covered_call", _COVERED_CALL, schema=schema)

    bad = [dict(_COVERED_CALL[0], option_right="C"), dict(_COVERED_CALL[1])]
    with pytest.raises(ValueError, match="stock leg"):
        structure_type_schema.validate_legs("covered_call", bad, schema=schema)
