"""The active-strategy writer updates only the columns it is told to (debt TD-38)."""

from __future__ import annotations

from typing import Any, List, Tuple

import pytest

import bifrost_core.monitor.reader.settings as settings
from bifrost_core.monitor.reader import write_support as ws


class _Cur:
    def __init__(self, log: List[Tuple[str, Any]]) -> None:
        self.log = log

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append((" ".join(sql.split()), params))

    def fetchone(self) -> Any:
        return (1,)

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self, log: List[Tuple[str, Any]]) -> None:
        self.log = log

    def cursor(self, **_: Any) -> _Cur:
        return _Cur(self.log)

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[str, Any]]:
    seen: List[Tuple[str, Any]] = []
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: _Conn(seen))
    return seen


def _updates(log: List[Tuple[str, Any]]) -> List[Tuple[str, Any]]:
    return [x for x in log if x[0].startswith("UPDATE settings")]


def test_only_the_named_column_is_written(log: List[Tuple[str, Any]]) -> None:
    ok = settings.write_active_strategy_and_gates(
        {"sink": "postgres"}, active_strategy_allocation_id=7, only={"active_strategy_allocation_id"}
    )
    assert ok
    assert _updates(log) == [("UPDATE settings SET active_strategy_allocation_id = %s WHERE id = 1", (7,))]


def test_without_only_all_three_are_written(log: List[Tuple[str, Any]]) -> None:
    settings.write_active_strategy_and_gates({"sink": "postgres"}, active_strategy_allocation_id=7)
    sql, params = _updates(log)[0]
    assert "active_strategy_structure_id = %s" in sql and "active_gate_safety_strategy_id = %s" in sql
    assert params == (None, None, 7)
