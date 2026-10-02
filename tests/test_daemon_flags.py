"""DEV's daemon keeps out of the shared Golden Source under either flag name (TD-22)."""

from __future__ import annotations

import pytest

from bifrost_core.core.daemon_flags import daemon_broker_writes_off


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAEMON_BROKER_WRITES_OFF", raising=False)
    monkeypatch.delenv("ACCOUNT_SYNC_DAEMON_ENABLED", raising=False)


def test_off_by_default() -> None:
    assert daemon_broker_writes_off() is False


@pytest.mark.parametrize("name", ["DAEMON_BROKER_WRITES_OFF", "ACCOUNT_SYNC_DAEMON_ENABLED"])
@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_either_name_turns_writes_off(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    assert daemon_broker_writes_off() is True


def test_other_values_leave_writes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEMON_BROKER_WRITES_OFF", "0")
    assert daemon_broker_writes_off() is False
