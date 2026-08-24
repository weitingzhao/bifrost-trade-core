"""Wave 9 reader SQL shape tests (no live PostgreSQL)."""

from __future__ import annotations

import inspect

from bifrost_core.monitor.reader import gate_safety
from bifrost_core.monitor.reader import gate_safety_write
from bifrost_core.monitor.reader import strategy_structure_write
from bifrost_core.monitor.reader import template_config_write


def test_gate_safety_select_uses_params_json():
    assert "params_json" in gate_safety._GATE_SAFETY_SELECT
    assert "min_dte" not in gate_safety._GATE_SAFETY_SELECT
    assert "epsilon_band" not in gate_safety._GATE_SAFETY_SELECT


def test_gate_safety_write_uses_params_json():
    src = inspect.getsource(gate_safety_write.create_gate_safety)
    assert "params_json" in src
    assert "gate_safety_strategy_earnings_dates" not in src


def test_template_legs_write_uses_legs_json():
    src = inspect.getsource(template_config_write.replace_template_legs)
    assert "legs_json" in src
    assert "strategy_template_leg" not in src


def test_structure_legs_write_uses_legs_json():
    src = inspect.getsource(strategy_structure_write._write_legs_json)
    assert "legs_json" in src
    assert "strategy_structure_leg" not in src
