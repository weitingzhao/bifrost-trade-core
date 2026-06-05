"""Unit tests for src.monitor.self_check (daemon merge + health roll-up)."""

from __future__ import annotations

from bifrost_core.monitor.self_check import derive_daemon_self_check, derive_health_roll_up


def _hb(*, alive: bool = True, ib: bool = True) -> dict:
    return {
        "last_ts": 1_700_000_000.0,
        "daemon_alive": alive,
        "ib_connected": ib,
    }


def test_daemon_merges_auto_row_data_stale() -> None:
    row = {
        "daemon_state": "RUNNING",
        "trading_state": "NORMAL",
        "data_lag_ms": 99_999.0,
    }
    out = derive_daemon_self_check(
        _hb(),
        auto_status_row=row,
        data_lag_threshold_ms=5000.0,
        trading_suspended=False,
    )
    assert out["daemon_self_check"] == "degraded"
    assert "data_stale" in out["daemon_block_reasons"]


def test_daemon_trading_state_in_daemon_self_check() -> None:
    row = {"daemon_state": "RUNNING", "trading_state": "RISK_HALT", "data_lag_ms": 0.0}
    out = derive_daemon_self_check(
        _hb(),
        auto_status_row=row,
        data_lag_threshold_ms=5000.0,
        trading_suspended=False,
    )
    assert out["daemon_self_check"] == "degraded"
    assert any("trading_state" in r for r in out["daemon_block_reasons"])


def test_health_roll_up_celery_no_workers() -> None:
    hc = derive_health_roll_up(
        daemon_lamp="green",
        daemon_block_reasons=[],
        monitor_lamp="green",
        monitor_block_reasons=[],
        massive=None,
        ib_ingestor=None,
        quotes_redis_reader_ok=True,
        celery_broker_connected=True,
        celery_workers=[],
        ib_account_agent=None,
    )
    assert hc["self_check"] == "degraded"
    assert "celery_no_workers" in hc["block_reasons"]


def test_health_roll_up_quotes_redis_down() -> None:
    hc = derive_health_roll_up(
        daemon_lamp="green",
        daemon_block_reasons=[],
        monitor_lamp="green",
        monitor_block_reasons=[],
        massive=None,
        ib_ingestor=None,
        quotes_redis_reader_ok=False,
        celery_broker_connected=True,
        celery_workers=["w1"],
        ib_account_agent=None,
    )
    assert hc["self_check"] == "degraded"
    assert "market_quotes_redis_unavailable" in hc["block_reasons"]
