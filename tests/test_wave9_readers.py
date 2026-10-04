"""Wave 9 reader SQL shape tests (no live PostgreSQL)."""

from __future__ import annotations

import inspect

from bifrost_core.monitor.reader import gate_safety
from bifrost_core.monitor.reader import gate_safety_write
from bifrost_core.monitor.reader import strategy_dim_catalog
from bifrost_core.monitor.reader import strategy_structure_write
from bifrost_core.monitor.reader import template_config
from bifrost_core.monitor.reader import template_config_write
from bifrost_core.monitor.reader.common import StatusReader


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


def test_template_legs_read_uses_only_legs_json():
    src = inspect.getsource(template_config.get_template_legs)
    assert "legs_json" in src
    assert "strategy_template_leg" not in src


def test_dims_come_from_the_catalog_without_a_database(monkeypatch):
    # No strategy_dim probe: the reader never connects to list dims.
    reader = StatusReader({"sink": "postgres"})

    def _no_connect() -> bool:
        raise AssertionError("list_dims_* must not open a connection")

    monkeypatch.setattr(reader, "_connect", _no_connect)
    grouped = reader.list_dims_grouped()
    assert grouped == strategy_dim_catalog.list_dims_grouped()
    assert {"strategy_dim_id", "dim_type", "code", "display_label", "sort_order"} <= set(grouped["direction"][0])
    assert not hasattr(template_config, "list_dims_grouped")
