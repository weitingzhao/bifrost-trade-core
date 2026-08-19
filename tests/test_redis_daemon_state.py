"""Unit tests for Redis daemon IPC helpers (no Redis required)."""

from __future__ import annotations

from bifrost_core.persistence.redis_daemon_state import (
    _decode_hash,
    _encode_mapping,
    account_sync_heartbeat_from_state,
    trading_heartbeat_from_state,
    trading_status_current_from_state,
)


def test_encode_decode_roundtrip_bools_and_lists() -> None:
    encoded = _encode_mapping(
        {
            "hedge_running": True,
            "ib_connected": False,
            "ib_client_id": 42,
            "subscribed_tickers": ["SPY", "QQQ"],
            "last_control_message": "",
            "spot": 1.25,
        }
    )
    assert encoded["hedge_running"] == "1"
    assert encoded["ib_connected"] == "0"
    assert encoded["ib_client_id"] == "42"
    assert encoded["subscribed_tickers"] == '["SPY","QQQ"]'
    decoded = _decode_hash(encoded)
    assert decoded["hedge_running"] is True
    assert decoded["ib_connected"] is False
    assert decoded["ib_client_id"] == 42
    assert decoded["subscribed_tickers"] == ["SPY", "QQQ"]
    assert decoded["spot"] == 1.25


def test_trading_heartbeat_shape() -> None:
    hb = trading_heartbeat_from_state(
        {
            "last_ts": 100.0,
            "hedge_running": True,
            "ib_connected": True,
            "ib_client_id": 7,
            "heartbeat_interval_sec": 10,
            "redis_quotes_connected": True,
            "mock_hedging": True,
            "subscribed_tickers": ["AAPL"],
        }
    )
    assert hb is not None
    assert hb["last_ts"] == 100.0
    assert hb["ib_client_id"] == 7
    assert hb["subscribed_tickers"] == ["AAPL"]
    assert trading_heartbeat_from_state(None) is None


def test_trading_status_and_account_sync_shapes() -> None:
    status = trading_status_current_from_state(
        {"daemon_state": "running", "symbol": "SPY", "ts": 1.0, "spot": 500.0}
    )
    assert status is not None
    assert status["daemon_auto_status_current_id"] == 1
    assert status["symbol"] == "SPY"
    assert trading_status_current_from_state({}) is None

    ash = account_sync_heartbeat_from_state(
        {
            "last_ts": 50.0,
            "last_sync_version": 3,
            "accounts_synced": 1,
            "alive": True,
            "heartbeat_interval_sec": 5.0,
        }
    )
    assert ash is not None
    assert ash["last_sync_version"] == 3
    assert ash["alive"] is True
