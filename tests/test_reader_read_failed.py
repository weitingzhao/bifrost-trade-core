"""A failed list read raises ReadFailed; it is never an empty list (debt TD-08)."""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_core.monitor.reader import gate_safety, strategy, strategy_instance
from bifrost_core.monitor.reader.common import StatusReader
from bifrost_core.monitor.reader.errors import ReadFailed


class _BrokenCur:
    def execute(self, *_: Any, **__: Any) -> None:
        raise RuntimeError("canceling statement due to statement timeout")

    def __enter__(self) -> "_BrokenCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _BrokenConn:
    def cursor(self, **_: Any) -> _BrokenCur:
        return _BrokenCur()


@pytest.mark.parametrize(
    "read",
    [
        lambda c: strategy.list_structures(c),
        lambda c: strategy.list_opportunities(c),
        lambda c: strategy.list_allocations(c),
        lambda c: gate_safety.list_gate_safety_sets(c),
        lambda c: strategy_instance.list_instances(c),
    ],
    ids=["structures", "opportunities", "allocations", "gate_sets", "instances"],
)
def test_module_reader_raises(read: Any) -> None:
    with pytest.raises(ReadFailed, match="statement timeout"):
        read(_BrokenConn())


@pytest.mark.parametrize(
    "method", ["list_structures", "list_opportunities", "list_allocations", "list_gate_safety_sets", "list_trades"]
)
def test_status_reader_raises_when_it_cannot_connect(monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    reader = StatusReader.__new__(StatusReader)
    monkeypatch.setattr(reader, "_connect", lambda: False, raising=False)
    with pytest.raises(ReadFailed, match="database unavailable"):
        getattr(reader, method)()
