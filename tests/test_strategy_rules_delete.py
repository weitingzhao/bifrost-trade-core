"""Deleting the Desk's rule objects: refused while in use, gone otherwise.

Against a fake connection, as `test_strategy_plan.py` does it.
"""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

from bifrost_core.monitor.reader import strategy_rules_delete as rules
from bifrost_core.monitor.reader.strategy_rules_delete import RuleInUseError

CFG = {"sink": "postgres"}


class _FakeCursor:
    def __init__(self, results: List[Any]) -> None:
        self._results = list(results)
        self.executed: List[tuple] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        return self._results.pop(0) if self._results else None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, results: Optional[List[Any]] = None) -> None:
        self.cur = _FakeCursor(results or [])
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, **_: Any) -> _FakeCursor:
        return self.cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch):
    def _make(results: Optional[List[Any]] = None) -> _FakeConn:
        fake = _FakeConn(results)
        monkeypatch.setattr(rules, "_conn_from_config", lambda _cfg: fake)
        return fake

    return _make


def _deleted(fake: _FakeConn) -> bool:
    return any(sql.startswith("DELETE") for sql, _ in fake.cur.executed)


def test_an_opportunity_with_trades_stays(conn) -> None:
    fake = conn([(1,), (2,)])
    with pytest.raises(RuleInUseError, match="It has 2 trades"):
        rules.delete_opportunity(CFG, 5)
    assert fake.commits == 0 and not _deleted(fake)


def test_an_opportunity_without_trades_goes(conn) -> None:
    fake = conn([(1,), (0,)])
    assert rules.delete_opportunity(CFG, 5) is True
    assert fake.commits == 1
    assert fake.cur.executed[-1] == ("DELETE FROM strategy_opportunity WHERE strategy_opportunity_id = %s", (5,))


def test_the_active_allocation_stays(conn) -> None:
    fake = conn([(1,), (True,)])
    with pytest.raises(RuleInUseError, match="active allocation"):
        rules.delete_allocation(CFG, 3)
    assert not _deleted(fake)


def test_an_inactive_allocation_goes(conn) -> None:
    fake = conn([(1,), (False,)])
    assert rules.delete_allocation(CFG, 3) is True
    assert fake.commits == 1


def test_a_gate_set_in_use_names_its_users(conn) -> None:
    conn([(1,), (1,), (0,)])
    with pytest.raises(RuleInUseError, match="^1 opportunity uses it"):
        rules.delete_gate_safety(CFG, 9)
    conn([(1,), (2,), (1,)])
    with pytest.raises(RuleInUseError, match="^2 opportunities and 1 allocation use it"):
        rules.delete_gate_safety(CFG, 9)


def test_an_unused_gate_set_goes(conn) -> None:
    fake = conn([(1,), (0,), (0,)])
    assert rules.delete_gate_safety(CFG, 9) is True
    assert fake.cur.executed[-1] == ("DELETE FROM gate_safety_strategy WHERE gate_safety_strategy_id = %s", (9,))


def test_a_missing_object_is_not_an_error(conn) -> None:
    fake = conn([None])
    assert rules.delete_allocation(CFG, 404) is False
    assert not _deleted(fake)
