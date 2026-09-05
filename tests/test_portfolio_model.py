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
    SPOT_SHOCKS,
    _group_positions,
    _years_to,
    _aggregate_stress,
    shares_backing_short_calls,
    _forward_returns,
)
from datetime import date, timedelta

import pytest


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
        assert len(result["scenarios"]) == len(SPOT_SHOCKS)

    def test_grid_spans_the_portfolio_margin_range_and_includes_a_baseline(self):
        # Portfolio margin stresses equities over +/-15%; without the 0 row there
        # is nothing for the other rows to be measured against.
        assert min(SPOT_SHOCKS) == -0.15
        assert max(SPOT_SHOCKS) == 0.15
        assert 0.0 in SPOT_SHOCKS

    def test_pnl_change_is_measured_from_the_unshocked_row(self):
        # Short 100 put at 3.00/share, spot 100, no stock.
        positions = [RiskPosition(strike=100, right="P", qty=-1, avg_cost=3.0)]
        result = _stress_matrix(positions, 0, None, 100.0, None, {})
        by_shock = {s["spot_shock"]: s for s in result["scenarios"]}

        # At spot the put expires worthless and the premium is kept.
        assert by_shock[0.0]["total_pnl"] == 300.0
        assert by_shock[0.0]["pnl_change"] == 0.0

        # 15% down puts it 15 in the money: 3.00 collected less 15.00 intrinsic.
        assert by_shock[-0.15]["total_pnl"] == -1200.0
        assert by_shock[-0.15]["pnl_change"] == -1500.0

        # Upside is capped at the premium, so the shock changes nothing.
        assert by_shock[0.15]["pnl_change"] == 0.0

    def test_pnl_change_separates_a_payoff_from_a_stress_reading(self):
        # 100 long shares bought at 50 with spot at 100: the payoff at any shocked
        # price is still a large gain, while the shock itself is a loss. Reporting
        # only the first is what made a 15% drop look like a five-figure profit.
        positions: list[RiskPosition] = []
        result = _stress_matrix(positions, 100, 50.0, 100.0, None, {})
        by_shock = {s["spot_shock"]: s for s in result["scenarios"]}
        assert by_shock[-0.15]["total_pnl"] == 3500.0
        assert by_shock[-0.15]["pnl_change"] == -1500.0

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


class TestOptionCostBasisLoading:
    """The per-contract / per-share boundary, pinned.

    IB reports an option's ``avgCost`` per contract; ``RiskPosition.avg_cost`` is
    declared per share and every consumer multiplies by 100. Loading it verbatim
    silently inflated the option side of every payoff, CAR and stress figure by
    100x — the kind of defect that reaches the screen as a confident number
    rather than as an error, so it gets a test rather than a comment.
    """

    def _row(self, **over):
        row = {
            "symbol": "DAVE",
            "sec_type": "OPT",
            "position": -1,
            "strike": 280.0,
            "option_right": "C",
            "avg_cost": 8415.776,  # per contract, as IB reports it
            "expiry": "20270115",
        }
        row.update(over)
        return row

    def test_per_contract_cost_becomes_per_share(self):
        groups = _group_positions([self._row()])
        leg = groups["DAVE"]["opt_positions"][0]
        assert leg.avg_cost == pytest.approx(84.15776)

    def test_the_payoff_that_used_to_be_100x(self):
        # 100 shares at 182.38 plus one short 280 call for 8,415.78 a contract.
        rows = [
            {
                "symbol": "DAVE",
                "sec_type": "STK",
                "position": 100,
                "avg_cost": 182.38,
                "price_last": 211.05,
            },
            self._row(),
        ]
        groups = _group_positions(rows)
        g = groups["DAVE"]
        legs = g["opt_positions"]
        # Max gain of a covered call: stock to the strike, plus the premium.
        gain = (280.0 - 182.38) * 100 + legs[0].avg_cost * 1 * 100
        assert gain == pytest.approx(18177.78, abs=0.01)
        # Before the fix this read 851,339.60.
        assert gain < 20_000

    def test_a_zero_cost_leg_is_still_loaded(self):
        groups = _group_positions([self._row(avg_cost=0.0)])
        assert groups["DAVE"]["opt_positions"][0].avg_cost == 0.0

    def test_stock_cost_basis_is_untouched(self):
        # Stock avgCost is already per share; dividing it would be the same
        # error in the other direction.
        groups = _group_positions(
            [{"symbol": "AAA", "sec_type": "STK", "position": 10, "avg_cost": 55.5}]
        )
        assert groups["AAA"]["stock_avg_cost"] == pytest.approx(55.5)


class TestPerLegExpiry:
    """Time value is priced to each leg's own expiry, not the group's farthest.

    A rolled position holds two expiries on one underlying. Pricing the near leg
    to the far date overstates its time value, and the error is largest exactly
    where a seller is most active.
    """

    def test_loader_carries_each_legs_expiry(self):
        rows = [
            {
                "symbol": "AAA", "sec_type": "OPT", "position": -1,
                "strike": 100.0, "option_right": "C", "avg_cost": 500.0,
                "expiry": "20261016",
            },
            {
                "symbol": "AAA", "sec_type": "OPT", "position": -1,
                "strike": 110.0, "option_right": "C", "avg_cost": 300.0,
                "expiry": "20270115",
            },
        ]
        legs = _group_positions(rows)["AAA"]["opt_positions"]
        assert [leg.expiry for leg in legs] == [date(2026, 10, 16), date(2027, 1, 15)]
        # And the group still knows its farthest, for callers that want it.
        assert _group_positions(rows)["AAA"]["farthest_expiry"] == date(2027, 1, 15)

    def test_years_to_uses_the_leg_and_floors_at_expiry(self):
        assert _years_to(None) == 0.0
        assert _years_to(date.today() - timedelta(days=5)) == 0.0
        assert _years_to(date.today() + timedelta(days=365)) == pytest.approx(1.0)

    def test_hedged_reconstruction_keeps_the_expiry(self):
        # _hedged_positions rebuilds legs; dropping expiry there would silently
        # restore the group-wide date for the hedged view only.
        p = RiskPosition(
            strike=100.0, right="C", qty=-2, avg_cost=5.0, expiry=date(2026, 10, 16)
        )
        assert p.expiry == date(2026, 10, 16)
        # Default keeps every existing constructor working.
        assert RiskPosition(strike=1.0, right="P", qty=1, avg_cost=1.0).expiry is None


class TestAggregateStressCoverage:
    """An account total must say how much of the account it covers.

    Underlyings without option mids emit only the iv_shock = 0 rows, so the
    IV-shocked account rows are summed over fewer symbols than the unshocked
    ones. The sum is still the sum; calling the two comparable is the error.
    """

    def _entry(self, iv_rows: bool):
        scenarios = [
            {"spot_shock": -0.15, "iv_shock": 0.0, "total_pnl": -100.0, "pnl_change": -50.0},
        ]
        if iv_rows:
            scenarios.append(
                {"spot_shock": -0.15, "iv_shock": 0.05, "total_pnl": -120.0, "pnl_change": -70.0}
            )
        return {"stress": {"available": True, "iv_stress_available": iv_rows, "scenarios": scenarios}}

    def test_a_row_missing_an_underlying_is_marked_partial(self):
        agg = _aggregate_stress([self._entry(True), self._entry(False)])
        by_key = {(s["spot_shock"], s["iv_shock"]): s for s in agg["scenarios"]}

        full = by_key[(-0.15, 0.0)]
        assert full["contributors"] == 2
        assert full["partial"] is False

        thin = by_key[(-0.15, 0.05)]
        assert thin["contributors"] == 1
        assert thin["partial"] is True
        assert agg["underlyings"] == 2

    def test_nothing_is_partial_when_every_underlying_contributes(self):
        agg = _aggregate_stress([self._entry(True), self._entry(True)])
        assert all(not s["partial"] for s in agg["scenarios"])

    def test_unavailable_underlyings_are_not_counted_as_coverage(self):
        agg = _aggregate_stress([self._entry(True), {"stress": {"available": False}}])
        assert agg["underlyings"] == 1
        assert all(not s["partial"] for s in agg["scenarios"])


class TestCoveredShareScope:
    """The payoff and CAR must describe the same shares.

    CAR counts only stock backing short calls. The payoff used to model the whole
    position, so on a name holding more stock than the calls need it carried an
    outright long's upside — and annualized_return_on_car, being one over the
    other, reported thousands of percent.
    """

    def _call(self, contracts: int) -> RiskPosition:
        return RiskPosition(strike=90.0, right="C", qty=-contracts, avg_cost=3.69)

    def test_extra_stock_beyond_the_calls_is_not_modelled(self):
        # RKLB's shape: 2,600 shares held, 10 short calls needing 1,000.
        assert shares_backing_short_calls([self._call(10)], 2600) == 1000

    def test_stock_is_the_binding_constraint_when_it_is_short(self):
        # NVDA's shape: 9 short calls want 900, only 500 shares held.
        assert shares_backing_short_calls([self._call(9)], 500) == 500

    def test_a_naked_call_covers_nothing(self):
        assert shares_backing_short_calls([self._call(1)], 0) == 0

    def test_long_calls_offset_the_requirement(self):
        legs = [self._call(3), RiskPosition(strike=100.0, right="C", qty=2, avg_cost=1.0)]
        assert shares_backing_short_calls(legs, 5000) == 100

    def test_puts_require_no_stock_cover(self):
        put = RiskPosition(strike=150.0, right="P", qty=-2, avg_cost=10.47)
        assert shares_backing_short_calls([put], 1000) == 0

    def test_negative_stock_does_not_produce_negative_cover(self):
        assert shares_backing_short_calls([self._call(1)], -400) == 0

    def test_the_ratio_the_scope_mismatch_used_to_inflate(self):
        # 2,600 shares at 19.79, 10 short 90 calls at 3.69/share.
        legs = [self._call(10)]
        covered = shares_backing_short_calls(legs, 2600)
        scoped = compute_risk_profile(legs, covered, 19.79)
        whole = compute_risk_profile(legs, 2600, 19.79)

        # Capital at risk counts the covered stock either way.
        car = 19.79 * covered
        assert car == pytest.approx(19_790.0)

        # Scoped, the return is a covered call's: strike less cost, plus premium.
        assert scoped.max_gain == pytest.approx((90.0 - 19.79) * 1000 + 3.69 * 10 * 100, abs=1)
        assert scoped.max_gain / car < 5

        # Unscoped it carried 1,600 uncovered shares' appreciation.
        assert whole.max_gain > scoped.max_gain * 4

    def test_naked_call_detection_is_unchanged_by_the_scope(self):
        legs = [self._call(10)]
        covered = shares_backing_short_calls(legs, 2600)
        assert compute_risk_profile(legs, covered, 19.79).naked_short_call_contracts == 0
        assert compute_risk_profile([self._call(1)], 0, None).naked_short_call_contracts == 1


class TestForwardReturns:
    """Returns measured from here, over capital committed now.

    max_gain measures from cost basis — right for a payoff diagram, wrong for
    "is this capital well placed", because on long-held stock most of it is
    already earned. RKLB read 1,311% a year that way against 18% on the premium.
    """

    RKLB = RiskPosition(strike=90.0, right="C", qty=-10, avg_cost=3.69)
    SPOT = 71.885
    COVERED = 1000
    DTE = 104

    def _fwd(self, committed, **kw):
        args = dict(
            opt_positions=[self.RKLB], covered_shares=self.COVERED,
            spot=self.SPOT, committed=committed, dte_days=self.DTE,
        )
        args.update(kw)
        return _forward_returns(**args)

    def test_static_is_the_premium_over_committed_capital(self):
        committed = self.SPOT * self.COVERED  # 71,885
        r = self._fwd(committed)
        # 3,690 premium on 71,885 over 104 days.
        assert r["static"] == pytest.approx((3690 / committed) * (365 / self.DTE), abs=1e-4)
        assert 0.15 < r["static"] < 0.20

    def test_if_called_adds_only_the_move_from_here_to_the_strike(self):
        committed = self.SPOT * self.COVERED
        r = self._fwd(committed)
        expected = ((3690 + (90.0 - self.SPOT) * self.COVERED) / committed) * (365 / self.DTE)
        assert r["if_called"] == pytest.approx(expected, abs=1e-4)
        assert 1.0 < r["if_called"] < 1.2
        # And it is nowhere near the cost-basis figure it replaces.
        assert r["if_called"] < 2.0

    def test_a_short_put_has_no_if_called_upside_beyond_the_premium(self):
        put = RiskPosition(strike=150.0, right="P", qty=-1, avg_cost=10.47)
        r = _forward_returns([put], 0, 140.0, 15000.0, 90)
        assert r["static"] == r["if_called"]

    def test_a_long_leg_subtracts_its_premium(self):
        long_call = RiskPosition(strike=100.0, right="C", qty=1, avg_cost=2.0)
        r = _forward_returns([long_call], 0, 95.0, 10000.0, 30)
        assert r["static"] is not None and r["static"] < 0

    def test_assignment_walks_the_lowest_strikes_first(self):
        # 500 covered shares against two short calls wanting 1,000: the 80 strike
        # is assigned before the 120 one.
        legs = [
            RiskPosition(strike=120.0, right="C", qty=-5, avg_cost=1.0),
            RiskPosition(strike=80.0, right="C", qty=-5, avg_cost=1.0),
        ]
        r = _forward_returns(legs, 500, 100.0, 50_000.0, 365)
        premium = 1.0 * 10 * 100
        expected = ((premium + (80.0 - 100.0) * 500) / 50_000.0)
        assert r["if_called"] == pytest.approx(expected, abs=1e-4)

    def test_no_capital_or_no_horizon_yields_nothing(self):
        assert self._fwd(None)["static"] is None
        assert self._fwd(0.0)["static"] is None
        assert self._fwd(1000.0, dte_days=0)["if_called"] is None

    def test_no_mark_falls_back_to_static_rather_than_inventing_a_move(self):
        r = self._fwd(71885.0, spot=None)
        assert r["static"] is not None
        assert r["if_called"] == r["static"]


class TestCarIsCommittedCapital:
    def test_a_covered_call_commits_the_shares_market_value(self):
        legs = [RiskPosition(strike=90.0, right="C", qty=-10, avg_cost=3.69)]
        car = _compute_car(legs, 2600, 71.885, None)
        # 1,000 shares covered at 71.885 — not the 19.79 they once cost.
        assert car["effective"] == pytest.approx(71_885.0)

    def test_a_cash_secured_put_still_commits_the_strike_notional(self):
        legs = [RiskPosition(strike=150.0, right="P", qty=-2, avg_cost=10.47)]
        assert _compute_car(legs, 0, 140.0, None)["effective"] == pytest.approx(30_000.0)

    def test_a_naked_call_is_unbounded(self):
        legs = [RiskPosition(strike=1200.0, right="C", qty=-1, avg_cost=54.99)]
        assert _compute_car(legs, 0, 200.0, None)["has_unbounded"] is True

    def test_no_mark_reports_no_capital_rather_than_the_cost_basis(self):
        legs = [RiskPosition(strike=90.0, right="C", qty=-1, avg_cost=3.69)]
        car = _compute_car(legs, 100, None, None)
        assert car["effective"] is None or car["effective"] == 0
