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
