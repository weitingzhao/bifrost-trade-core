"""Unit tests for bifrost_core.monitor.self_check (daemon merge + health roll-up)."""

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


def test_health_roll_up_all_green() -> None:
    hc = derive_health_roll_up(
        daemon_lamp="green",
        daemon_block_reasons=[],
        monitor_lamp="green",
        monitor_block_reasons=[],
        ib_ingestor=None,
        quotes_redis_reader_ok=True,
        ib_account_agent=None,
    )
    assert hc["self_check"] == "ok"
    assert hc["block_reasons"] == []


def test_health_roll_up_quotes_redis_down() -> None:
    hc = derive_health_roll_up(
        daemon_lamp="green",
        daemon_block_reasons=[],
        monitor_lamp="green",
        monitor_block_reasons=[],
        ib_ingestor=None,
        quotes_redis_reader_ok=False,
        ib_account_agent=None,
    )
    assert hc["self_check"] == "degraded"
    assert "market_quotes_redis_unavailable" in hc["block_reasons"]


# --- TD-76 (core 0.35.0): daemon_alive = heartbeat younger than max(35 s, 3 x interval) ---

import pytest  # noqa: E402

from bifrost_core.monitor.self_check import daemon_alive_threshold_sec, is_daemon_alive  # noqa: E402


@pytest.mark.parametrize(
    "interval,threshold",
    [
        (None, 35.0),  # missing -> default 10 s -> the old fixed 35 s
        (10, 35.0),
        ("10", 35.0),
        (5, 35.0),
        (11, 35.0),
        (12, 36.0),
        (30, 90.0),
        (120, 360.0),
        (500, 360.0),  # clamped to 120 s like the daemon
        (1, 35.0),  # clamped to 5 s
        (0, 35.0),
        (-3, 35.0),
        ("x", 35.0),
        (float("nan"), 35.0),
    ],
)
def test_daemon_alive_threshold(interval, threshold) -> None:
    assert daemon_alive_threshold_sec(interval) == threshold


def test_is_daemon_alive_uses_the_interval() -> None:
    now = 1_790_000_000.0
    assert is_daemon_alive(now - 34.9, 10, now)
    assert not is_daemon_alive(now - 35.0, 10, now)  # strict, as the old `< 35`
    assert not is_daemon_alive(now - 40.0, None, now)
    # a 30 s heartbeat 40 s old is one missed beat, not a dead daemon (was "dead" before 0.35.0)
    assert is_daemon_alive(now - 40.0, 30, now)
    assert not is_daemon_alive(now - 90.0, 30, now)
    assert not is_daemon_alive(None, 30, now)
    assert not is_daemon_alive("garbage", 30, now)
