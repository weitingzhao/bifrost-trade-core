"""Tests for config/settings: gates structure, get_hedge_config, get_structure_config."""

import pytest

from bifrost_core.config.settings import (
    get_hedge_config,
    get_state_space_config,
    get_structure_config,
    get_risk_config,
)


class TestGatesConfig:
    """Option 2 gates structure: gates.strategy, gates.state, gates.intent, gates.guard."""

    def test_get_hedge_config_from_gates(self):
        cfg = {
            "gates": {
                "state": {"delta": {"threshold_hedge_shares": 30}},
                "intent": {"hedge": {"min_hedge_shares": 15, "cooldown_seconds": 120}},
                "guard": {"risk": {"max_daily_hedge_count": 25, "max_position_shares": 1000}},
                "strategy": {
                    "earnings": {"blackout_days_before": 5, "dates": ["2025-03-01"]},
                    "trading_hours_only": False,
                },
            }
        }
        out = get_hedge_config(cfg)
        assert out["threshold_hedge_shares"] == 30
        assert out["min_hedge_shares"] == 15
        assert out["cooldown_sec"] == 120
        assert out["max_daily_hedge_count"] == 25
        assert out["max_position_shares"] == 1000
        assert out["trading_hours_only"] is False
        assert out["blackout_days_before"] == 5
        assert "2025-03-01" in out["earnings_dates"]

    def test_get_structure_config_from_gates(self):
        cfg = {
            "gates": {
                "strategy": {
                    "structure": {"min_dte": 14, "max_dte": 42, "atm_band_pct": 0.05},
                },
            }
        }
        out = get_structure_config(cfg)
        assert out["min_dte"] == 14
        assert out["max_dte"] == 42
        assert out["atm_band_pct"] == 0.05

    def test_get_risk_config_from_gates(self):
        cfg = {"gates": {"guard": {"risk": {"paper_trade": False, "max_spread_pct": 0.02}}}}
        out = get_risk_config(cfg)
        assert out["paper_trade"] is False
        assert out["max_spread_pct"] == 0.02

    def test_backward_compat_hedge_threshold(self):
        """Legacy hedge_threshold in config still supported."""
        cfg = {"delta": {"hedge_threshold": 40}}
        out = get_hedge_config(cfg)
        assert out["threshold_hedge_shares"] == 40

    def test_backward_compat_top_level(self):
        cfg = {
            "delta": {"threshold_hedge_shares": 40},
            "hedge": {"min_hedge_shares": 20},
            "risk": {"max_daily_hedge_count": 10},
            "earnings": {"blackout_days_after": 2},
        }
        out = get_hedge_config(cfg)
        assert out["threshold_hedge_shares"] == 40
        assert out["min_hedge_shares"] == 20
        assert out["max_daily_hedge_count"] == 10
        assert out["blackout_days_after"] == 2

    def test_get_state_space_config_from_gates(self):
        cfg = {
            "gates": {
                "state": {
                    "delta": {"epsilon_band": 15, "threshold_hedge_shares": 35},
                    "system": {"data_lag_threshold_ms": 2000},
                },
                "intent": {"hedge": {"min_price_move_pct": 0.5}},
            }
        }
        out = get_state_space_config(cfg)
        assert out["delta"]["epsilon_band"] == 15
        assert out["delta"]["threshold_hedge_shares"] == 35
        assert out["system"]["data_lag_threshold_ms"] == 2000
        assert out["hedge"]["min_price_move_pct"] == 0.5
