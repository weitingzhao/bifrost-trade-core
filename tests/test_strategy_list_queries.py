"""One SELECT per list, filtered on request; one reader for the settings references (TD-65)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from bifrost_core.monitor.reader import gate_safety, strategy


class _Cur:
    def __init__(self, row: Optional[Dict[str, Any]] = None) -> None:
        self.sql: List[str] = []
        self.row = row

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append(" ".join(sql.split()))

    def fetchall(self) -> List[Dict[str, Any]]:
        return []

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return self.row

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self, row: Optional[Dict[str, Any]] = None) -> None:
        self.cur = _Cur(row)

    def cursor(self, **_: Any) -> _Cur:
        return self.cur


@pytest.mark.parametrize(
    "fn, alias",
    [(strategy.list_structures, "s"), (strategy.list_opportunities, "o"), (strategy.list_allocations, "p")],
)
def test_active_only_adds_the_filter_and_nothing_else(fn: Any, alias: str) -> None:
    active, every = _Conn(), _Conn()
    fn(active, True)
    fn(every, False)
    a, e = active.cur.sql[0], every.cur.sql[0]
    assert f"WHERE {alias}.is_active = true" in a
    assert "is_active = true" not in e
    assert a.replace(f"WHERE {alias}.is_active = true ", "") == e


def test_settings_references_read_their_own_column() -> None:
    conn = _Conn({"active_strategy_allocation_id": 7})
    assert gate_safety.get_active_strategy_allocation_id(conn) == 7
    assert conn.cur.sql == ["SELECT active_strategy_allocation_id FROM settings WHERE id = 1"]
    assert gate_safety.get_active_gate_safety_strategy_id(_Conn({"active_gate_safety_strategy_id": None})) is None
    assert gate_safety.get_active_strategy_structure_id(_Conn(None)) is None


def test_settings_reader_refuses_other_columns() -> None:
    with pytest.raises(ValueError):
        gate_safety._settings_ref(_Conn(), "name; DROP TABLE settings")
