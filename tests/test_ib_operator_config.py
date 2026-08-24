"""Tests for ib_operator config defaults."""

from bifrost_core.ib_operator.config import effective_ib_operator_settings


def test_ib_operator_enabled_with_redis_ib() -> None:
    cfg = {"redis_ib": {"host": "127.0.0.1", "port": 6379}}
    s = effective_ib_operator_settings(cfg)
    assert s["enabled"] is True
    assert s["request_timeout_sec"] == 120.0
