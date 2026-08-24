"""Wave 9 migration idempotency tests (no live PostgreSQL)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from bifrost_core.persistence.postgres.wave9_migrations import (
    _GATE_FLAT_COLUMNS,
    _any_gate_flat_column_exists,
    _drop_gate_flat_columns,
    _existing_gate_flat_columns,
    _merge_flat_row_with_defaults,
    _migrate_gate_params,
    _params_json_is_empty,
)


def test_params_json_is_empty():
    assert _params_json_is_empty(None) is True
    assert _params_json_is_empty({}) is True
    assert _params_json_is_empty("{}") is True
    assert _params_json_is_empty({"strategy": {}}) is False


def test_existing_gate_flat_columns_partial():
    cur = MagicMock()

    def col_exists(_cur: object, table: str, column: str) -> bool:
        assert table == "gate_safety_strategy"
        return column in {"epsilon_band", "max_dte"}

    with patch(
        "bifrost_core.persistence.postgres.wave9_migrations._column_exists",
        side_effect=col_exists,
    ):
        assert _existing_gate_flat_columns(cur) == ("max_dte", "epsilon_band")
        assert _any_gate_flat_column_exists(cur) is True


def test_drop_gate_flat_columns_always_attempts_all_columns():
    cur = MagicMock()
    _drop_gate_flat_columns(cur)
    drop_sql = [
        args[0][0]
        for args in cur.execute.call_args_list
        if "DROP COLUMN IF EXISTS" in args[0][0]
    ]
    assert len(drop_sql) == len(_GATE_FLAT_COLUMNS)
    for col in _GATE_FLAT_COLUMNS:
        assert any(col in sql for sql in drop_sql)


def test_migrate_gate_params_with_partial_flat_columns():
    cur = MagicMock()
    existing = ("max_dte", "epsilon_band")
    row = (
        1,
        "Default",
        1,
        "long",
        None,
        None,
        None,
        None,
        None,
        True,
        "{}",
        40,
        15,
    )

    def col_exists(_cur: object, table: str, column: str) -> bool:
        if table != "gate_safety_strategy":
            return False
        return column in existing

    def table_exists(_cur: object, name: str) -> bool:
        return name == "strategy_template"

    fetchall_calls = {"count": 0}

    def fetchall():
        fetchall_calls["count"] += 1
        if fetchall_calls["count"] == 1:
            return [row]
        return []

    cur.fetchall.side_effect = fetchall

    with (
        patch(
            "bifrost_core.persistence.postgres.wave9_migrations._column_exists",
            side_effect=col_exists,
        ),
        patch(
            "bifrost_core.persistence.postgres.wave9_migrations._table_exists",
            side_effect=table_exists,
        ),
    ):
        _migrate_gate_params(cur)

    update_calls = [
        args
        for args in cur.execute.call_args_list
        if args[0][0].startswith("UPDATE gate_safety_strategy SET params_json")
    ]
    assert len(update_calls) == 1
    params = json.loads(update_calls[0][0][1][0])
    assert params["state"]["delta"]["epsilon_band"] == 15
    assert params["strategy"]["structure"]["max_dte"] == 40
    assert params["strategy"]["structure"]["min_dte"] == 21


def test_migrate_gate_params_skips_rows_with_populated_params_json():
    cur = MagicMock()
    existing = ("epsilon_band",)
    populated = json.dumps({"state": {"delta": {"epsilon_band": 99}}})
    row = (
        2,
        "Active",
        1,
        "long",
        None,
        None,
        None,
        None,
        None,
        True,
        populated,
        12,
    )

    def col_exists(_cur: object, table: str, column: str) -> bool:
        return table == "gate_safety_strategy" and column in existing

    cur.fetchall.return_value = [row]

    with patch(
        "bifrost_core.persistence.postgres.wave9_migrations._column_exists",
        side_effect=col_exists,
    ):
        _migrate_gate_params(cur)

    update_calls = [
        args
        for args in cur.execute.call_args_list
        if args[0][0].startswith("UPDATE gate_safety_strategy SET params_json")
    ]
    assert update_calls == []


def test_merge_flat_row_with_defaults_fills_missing_columns():
    merged = _merge_flat_row_with_defaults({"epsilon_band": 42, "max_dte": 30}, ("epsilon_band", "max_dte"))
    assert merged["epsilon_band"] == 42
    assert merged["max_dte"] == 30
    assert merged["min_dte"] == 21
