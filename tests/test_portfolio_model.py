"""Unit tests for servers.portfolio_model — payoff, CAR, annualization, BS/IV, stress."""

from bifrost_core.portfolio.model.payoff import (
    RiskPosition,
    compute_risk_profile,
    payoff_options_at_price,
    payoff_stock_at_price,
)
from bifrost_core.portfolio.model.core import (
    _compute_car,
    _annualized_return,
    _implied_vol,
    _bs_price,
    _bs_delta,
    _stress_matrix,
    _compute_greeks_for_group,
)
from datetime import date, timedelta


# ---------------------------------------------------------------------------
# Payoff engine
# ---------------------------------------------------------------------------

class TestPayoffOptions:
    def test_long_call_itm(self):
        positions = [RiskPosition(strike=100, right="C", qty=1, avg_cost=5.0)]
        assert payoff_options_at_price(positions, 120) == (120 - 100 - 5) * 100

    def test_long_call_otm(self):
        positions = [RiskPosition(strike=100, right="C", qty=1, avg_cost=5.0)]
        assert payoff_options_at_price(positions, 90) == -5 * 100

    def test_short_put_otm(self):
        positions = [RiskPosition(strike=100, right="P", qty=-1, avg_cost=3.0)]
        assert payoff_options_at_price(positions, 110) == 3 * 100

    def test_short_put_itm(self):
        positions = [RiskPosition(strike=100, right="P", qty=-1, avg_cost=3.0)]
        pnl = payoff_options_at_price(positions, 80)
        assert pnl == (3 - 20) * 100  # -1700


class TestPayoffStock:
    def test_positive_shares(self):
        assert payoff_stock_at_price(100, 50.0, 60.0) == 1000.0

    def test_no_shares(self):
        assert payoff_stock_at_price(0, None, 100.0) == 0.0


class TestRiskProfile:
    def test_bull_put_spread(self):
        positions = [
            RiskPosition(strike=100, right="P", qty=-1, avg_cost=3.0),
            RiskPosition(strike=95, right="P", qty=1, avg_cost=1.0),
        ]
        p = compute_risk_profile(positions, 0, None)
        assert p.risk_type == "defined"
        assert p.max_gain == 200.0
        assert p.max_loss == -300.0
        assert len(p.breakeven_prices) == 1

    def test_covered_call(self):
        positions = [RiskPosition(strike=110, right="C", qty=-1, avg_cost=2.0)]
        p = compute_risk_profile(positions, 100, 100.0)
        assert p.risk_type == "defined"
        assert p.max_gain is not None and p.max_gain > 0
        assert p.naked_short_call_contracts == 0

    def test_long_call_unlimited_upside(self):
        positions = [RiskPosition(strike=100, right="C", qty=1, avg_cost=5.0)]
        p = compute_risk_profile(positions, 0, None)
        assert p.max_gain is None  # unlimited
        assert p.max_loss == -500.0

    def test_naked_short_call(self):
        positions = [RiskPosition(strike=100, right="C", qty=-1, avg_cost=2.0)]
        p = compute_risk_profile(positions, 0, None)
        assert p.risk_type == "unlimited"
        assert p.naked_short_call_contracts == 1

    def test_empty_positions(self):
        p = compute_risk_profile([], 0, None)
        assert p.max_gain == 0.0
        assert p.max_loss == 0.0


# ---------------------------------------------------------------------------
# CAR
# ---------------------------------------------------------------------------

class TestCAR:
    def test_csp_car(self):
        positions = [RiskPosition(strike=100, right="P", qty=-1, avg_cost=3.0)]
        car = _compute_car(positions, 0, None, -9700.0)
        assert car["effective"] == 9700.0  # net portfolio max loss < leg CAR (10000)
        assert car["explain"] == "net_portfolio_max_loss"

    def test_spread_car(self):
        positions = [
            RiskPosition(strike=100, right="P", qty=-1, avg_cost=3.0),
            RiskPosition(strike=95, right="P", qty=1, avg_cost=1.0),
        ]
        car = _compute_car(positions, 0, None, -300.0)
        assert car["effective"] == 300.0

    def test_naked_call_unbounded(self):
        positions = [RiskPosition(strike=100, right="C", qty=-1, avg_cost=2.0)]
        car = _compute_car(positions, 0, None, None)
        assert car["has_unbounded"] is True
        assert car["effective"] is None


# ---------------------------------------------------------------------------
# Annualized return
# ---------------------------------------------------------------------------

class TestAnnualized:
    def test_basic(self):
        r = _annualized_return(200.0, 10000.0, 30)
        assert r is not None
        assert abs(r - (200 / 10000) * (365 / 30)) < 1e-4

    def test_zero_car(self):
        assert _annualized_return(200.0, 0.0, 30) is None

    def test_zero_dte(self):
        assert _annualized_return(200.0, 10000.0, 0) is None

    def test_none_profit(self):
        assert _annualized_return(None, 10000.0, 30) is None


# ---------------------------------------------------------------------------
# Black-Scholes
# ---------------------------------------------------------------------------

class TestBlackScholes:
    def test_call_price(self):
        p = _bs_price(100, 100, 30 / 365, 0.04, 0.30, "C")
        assert 2.5 < p < 5.0  # reasonable ATM call

    def test_put_price(self):
        p = _bs_price(100, 100, 30 / 365, 0.04, 0.30, "P")
        assert 2.0 < p < 4.5

    def test_expired(self):
        c = _bs_price(110, 100, 0, 0.04, 0.30, "C")
        assert c == 10.0
        p = _bs_price(90, 100, 0, 0.04, 0.30, "P")
        assert p == 10.0

    def test_iv_roundtrip(self):
        T = 30 / 365
        original_iv = 0.35
        price = _bs_price(100, 100, T, 0.04, original_iv, "C")
        recovered = _implied_vol(price, 100, 100, T, 0.04, "C")
        assert recovered is not None
        assert abs(recovered - original_iv) < 0.001

    def test_iv_deep_itm(self):
        iv = _implied_vol(50, 100, 50, 30 / 365, 0.04, "C")
        # Deep ITM — should still converge or return reasonable value
        # (intrinsic = 50, market = 50, so IV should be near 0)
        assert iv is None or iv < 0.1

    def test_delta_atm_call(self):
        d = _bs_delta(100, 100, 30 / 365, 0.04, 0.30, "C")
        assert 0.45 < d < 0.60

    def test_delta_atm_put(self):
        d = _bs_delta(100, 100, 30 / 365, 0.04, 0.30, "P")
        assert -0.55 < d < -0.40


# ---------------------------------------------------------------------------
# Stress matrix
# ---------------------------------------------------------------------------

class TestStress:
    def test_intrinsic_only(self):
        positions = [RiskPosition(strike=100, right="P", qty=-1, avg_cost=3.0)]
        result = _stress_matrix(positions, 0, None, 100.0, None, {})
        assert result["available"] is True
        assert not result["iv_stress_available"]
        assert len(result["scenarios"]) == 4  # 4 spot shocks

    def test_no_spot(self):
        positions = [RiskPosition(strike=100, right="P", qty=-1, avg_cost=3.0)]
        result = _stress_matrix(positions, 0, None, None, None, {})
        assert result["available"] is False

    def test_with_iv(self):
        expiry = date.today() + timedelta(days=30)
        positions = [RiskPosition(strike=100, right="P", qty=-1, avg_cost=3.0)]
        mids = {(100.0, "P"): 3.5}
        result = _stress_matrix(positions, 0, None, 100.0, expiry, mids)
        assert result["available"] is True
        if result["iv_stress_available"]:
            assert len(result["scenarios"]) > 4  # spot x (base + IV shocks)


# ---------------------------------------------------------------------------
# Greeks
# ---------------------------------------------------------------------------

class TestGreeks:
    def test_stock_only(self):
        g = _compute_greeks_for_group([], 100, 50.0, None, {})
        assert g["delta"] == 100.0
        assert g["delta_dollars"] == 5000.0
        assert g["degraded"] is False

    def test_no_spot(self):
        g = _compute_greeks_for_group(
            [RiskPosition(strike=100, right="C", qty=1, avg_cost=5.0)],
            0, None, None, {},
        )
        assert g["delta"] is None
        assert g["degraded"] is True
