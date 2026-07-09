"""Tests for bull/bear call spread structure type leg schemas."""

import pytest

from bifrost_core.monitor.reader import structure_type_schema


@pytest.mark.parametrize(
    ("structure_type", "long_idx", "short_idx"),
    [
        ("bull_call_spread", 0, 1),
        ("bear_call_spread", 1, 0),
    ],
)
def test_call_spread_default_legs(structure_type: str, long_idx: int, short_idx: int) -> None:
    legs = structure_type_schema.get_default_legs(structure_type)
    assert len(legs) == 2
    assert legs[long_idx]["role"] == "call"
    assert legs[long_idx]["direction"] == "long"
    assert legs[long_idx]["option_right"] == "C"
    assert legs[short_idx]["role"] == "call"
    assert legs[short_idx]["direction"] == "short"
    assert legs[short_idx]["option_right"] == "C"


def test_call_spread_validate_legs() -> None:
    legs = structure_type_schema.get_default_legs("bull_call_spread")
    structure_type_schema.validate_legs("bull_call_spread", legs)

    bad = [dict(legs[0]), dict(legs[1])]
    bad[1]["direction"] = "long"
    with pytest.raises(ValueError, match="direction"):
        structure_type_schema.validate_legs("bull_call_spread", bad)
