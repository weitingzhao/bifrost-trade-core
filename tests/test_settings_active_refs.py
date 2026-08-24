"""validate_settings_active_refs — settings active_* pointer guard."""

from unittest.mock import MagicMock

import pytest

from bifrost_core.monitor.reader.settings import validate_settings_active_refs


def test_validate_settings_active_refs_all_null_passes() -> None:
    cur = MagicMock()
    validate_settings_active_refs(
        cur,
        {
            "active_gate_safety_strategy_id": None,
            "active_strategy_structure_id": None,
            "active_strategy_allocation_id": None,
        },
    )
    cur.execute.assert_not_called()


def test_validate_settings_active_refs_all_exist_passes() -> None:
    cur = MagicMock()
    cur.fetchone.return_value = (1,)
    validate_settings_active_refs(
        cur,
        {
            "active_gate_safety_strategy_id": 10,
            "active_strategy_structure_id": 20,
            "active_strategy_allocation_id": 30,
        },
    )
    assert cur.execute.call_count == 3


def test_validate_settings_active_refs_missing_raises() -> None:
    cur = MagicMock()
    cur.fetchone.return_value = None
    with pytest.raises(ValueError, match="active_strategy_structure_id=99"):
        validate_settings_active_refs(
            cur,
            {"active_strategy_structure_id": 99},
        )
