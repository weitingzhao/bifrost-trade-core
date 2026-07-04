"""Tests for ib_operator config defaults (TIBM3)."""

from bifrost_core.ib_operator.config import effective_ib_operator_settings


def test_use_for_celery_bars_defaults_true() -> None:
    cfg = {"redis_ib": {"url": "redis://127.0.0.1:6379/0"}}
    s = effective_ib_operator_settings(cfg)
    assert s["use_for_celery_bars"] is True


def test_use_for_celery_bars_explicit_false() -> None:
    cfg = {
        "redis_ib": {"url": "redis://127.0.0.1:6379/0"},
        "ib_operator": {"use_for_celery_bars": False},
    }
    s = effective_ib_operator_settings(cfg)
    assert s["use_for_celery_bars"] is False


def test_use_for_celery_bars_explicit_true() -> None:
    cfg = {
        "redis_ib": {"url": "redis://127.0.0.1:6379/0"},
        "ib_operator": {"use_for_celery_bars": True},
    }
    s = effective_ib_operator_settings(cfg)
    assert s["use_for_celery_bars"] is True
