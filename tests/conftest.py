"""Shared pytest fixtures for bifrost-core."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def sample_config():
    return {
        "gates": {
            "state": {
                "delta": {
                    "threshold_hedge_shares": 25,
                    "epsilon_band": 10,
                    "max_delta_limit": 500,
                }
            }
        },
        "greeks": {
            "risk_free_rate": 0.05,
            "volatility": 0.35,
        },
    }


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def sample_yaml(project_root: Path) -> Path:
    return project_root / "config" / "config.yaml.example"
