"""Unit tests for flattened gate_safety_strategy (no live PostgreSQL)."""

from __future__ import annotations

from pathlib import Path

from bifrost_core.monitor.reader.gate_safety import _row_to_gates, build_gate_params_from_flat_row
from bifrost_core.monitor.reader.gate_safety_write import _payload_to_row, _STRATEGY_COLUMNS
from bifrost_core.persistence.postgres.ddl import _GATE_SAFETY_RETIRED_CHILD_TABLES


def test_ddl_source_does_not_create_retired_gate_safety_children():
    src = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "bifrost_core"
        / "persistence"
        / "postgres"
        / "ddl.py"
    ).read_text(encoding="utf-8")
    for name in _GATE_SAFETY_RETIRED_CHILD_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {name}" not in src, name


def test_payload_to_row_builds_params_json():
    payload = {
        "name": "Default",
        "version": 2,
        "dim_direction": "long",
        "is_active": True,
        "gates": {
            "strategy": {
                "structure": {"min_dte": 21, "max_dte": 35, "atm_band_pct": 0.03},
                "earnings": {"blackout_days_before": 3, "blackout_days_after": 1, "dates": ["2026-04-15"]},
                "trading_hours_only": True,
            },
            "state": {
                "delta": {"epsilon_band": 10, "threshold_hedge_shares": 25, "max_delta_limit": 500},
                "market": {"vol_window_min": 5, "stale_ts_threshold_ms": 5000},
                "liquidity": {"wide_spread_pct": 0.1, "extreme_spread_pct": 0.5},
                "system": {"data_lag_threshold_ms": 1000},
            },
            "intent": {
                "hedge": {
                    "min_hedge_shares": 10,
                    "cooldown_seconds": 60,
                    "max_hedge_shares_per_order": 500,
                    "min_price_move_pct": 0.2,
                }
            },
            "guard": {
                "risk": {
                    "max_daily_hedge_count": 50,
                    "max_position_shares": 2000,
                    "max_daily_loss_usd": 5000.0,
                    "max_net_delta_shares": 100,
                    "max_spread_pct": 0.05,
                    "paper_trade": True,
                }
            },
        },
        "earnings_dates": ["2026-04-15", "2026-07-20"],
    }
    row, dates = _payload_to_row(payload)
    assert set(_STRATEGY_COLUMNS) <= set(row.keys())
    assert row["name"] == "Default"
    params = row["params_json"]
    assert params["state"]["delta"]["epsilon_band"] == 10
    assert params["intent"]["hedge"]["min_hedge_shares"] == 10
    assert params["guard"]["risk"]["max_daily_hedge_count"] == 50
    assert params["guard"]["risk"]["paper_trade"] is True
    assert dates == ["2026-04-15", "2026-07-20"]


def test_row_to_gates_from_flat_row_preserves_config_shape():
    row = {
        "min_dte": 21,
        "max_dte": 35,
        "atm_band_pct": 0.03,
        "blackout_days_before": 3,
        "blackout_days_after": 1,
        "trading_hours_only": True,
        "epsilon_band": 10,
        "threshold_hedge_shares": 25,
        "max_delta_limit": 500,
        "vol_window_min": 5,
        "stale_ts_threshold_ms": 5000,
        "wide_spread_pct": 0.1,
        "extreme_spread_pct": 0.5,
        "data_lag_threshold_ms": 1000,
        "min_hedge_shares": 10,
        "cooldown_seconds": 60,
        "max_hedge_shares_per_order": 500,
        "min_price_move_pct": 0.2,
        "max_daily_hedge_count": 50,
        "max_position_shares": 2000,
        "max_daily_loss_usd": 5000.0,
        "max_net_delta_shares": 100,
        "max_spread_pct": 0.05,
        "paper_trade": True,
    }
    gates = _row_to_gates(row, ["2026-04-15"])
    assert set(gates.keys()) == {"strategy", "state", "intent", "guard"}
    assert gates["strategy"]["structure"]["min_dte"] == 21
    assert gates["strategy"]["earnings"]["dates"] == ["2026-04-15"]
    assert gates["state"]["delta"]["epsilon_band"] == 10
    assert gates["intent"]["hedge"]["cooldown_seconds"] == 60
    assert gates["guard"]["risk"]["paper_trade"] is True


def test_row_to_gates_from_params_json():
    params = build_gate_params_from_flat_row(
        {
            "min_dte": 14,
            "max_dte": 45,
            "atm_band_pct": 0.05,
            "blackout_days_before": 2,
            "blackout_days_after": 2,
            "trading_hours_only": False,
            "epsilon_band": 8,
            "threshold_hedge_shares": 20,
            "max_delta_limit": 400,
            "vol_window_min": 7,
            "stale_ts_threshold_ms": 4000,
            "wide_spread_pct": 0.12,
            "extreme_spread_pct": 0.4,
            "data_lag_threshold_ms": 800,
            "min_hedge_shares": 5,
            "cooldown_seconds": 30,
            "max_hedge_shares_per_order": 200,
            "min_price_move_pct": 0.1,
            "max_daily_hedge_count": 20,
            "max_position_shares": 1000,
            "max_daily_loss_usd": 2500.0,
            "max_net_delta_shares": 50,
            "max_spread_pct": 0.04,
            "paper_trade": False,
        },
        ["2026-01-01"],
    )
    gates = _row_to_gates({"params_json": params}, None)
    assert gates["strategy"]["structure"]["min_dte"] == 14
    assert gates["strategy"]["trading_hours_only"] is False
    assert gates["state"]["system"]["data_lag_threshold_ms"] == 800
    assert gates["intent"]["hedge"]["min_hedge_shares"] == 5
    assert gates["guard"]["risk"]["paper_trade"] is False
    assert gates["strategy"]["earnings"]["dates"] == ["2026-01-01"]


def test_payload_roundtrip_matches_row_to_gates():
    payload = {
        "name": "Roundtrip",
        "gates": {
            "strategy": {
                "structure": {"min_dte": 14, "max_dte": 45, "atm_band_pct": 0.05},
                "earnings": {"blackout_days_before": 2, "blackout_days_after": 2},
                "trading_hours_only": False,
            },
            "state": {
                "delta": {"epsilon_band": 8, "threshold_hedge_shares": 20, "max_delta_limit": 400},
                "market": {"vol_window_min": 7, "stale_ts_threshold_ms": 4000},
                "liquidity": {"wide_spread_pct": 0.12, "extreme_spread_pct": 0.4},
                "system": {"data_lag_threshold_ms": 800},
            },
            "intent": {
                "hedge": {
                    "min_hedge_shares": 5,
                    "cooldown_seconds": 30,
                    "max_hedge_shares_per_order": 200,
                    "min_price_move_pct": 0.1,
                }
            },
            "guard": {
                "risk": {
                    "max_daily_hedge_count": 20,
                    "max_position_shares": 1000,
                    "max_daily_loss_usd": 2500.0,
                    "max_net_delta_shares": 50,
                    "max_spread_pct": 0.04,
                    "paper_trade": False,
                }
            },
        },
        "earnings_dates": ["2026-01-01"],
    }
    row, dates = _payload_to_row(payload)
    gates = _row_to_gates(row, dates)
    assert gates["strategy"]["structure"]["min_dte"] == 14
    assert gates["strategy"]["trading_hours_only"] is False
    assert gates["state"]["system"]["data_lag_threshold_ms"] == 800
    assert gates["intent"]["hedge"]["min_hedge_shares"] == 5
    assert gates["guard"]["risk"]["paper_trade"] is False
    assert dates == ["2026-01-01"]
