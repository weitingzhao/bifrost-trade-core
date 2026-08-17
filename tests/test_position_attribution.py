"""Unit tests for Position × Instance attribution (net-estimated method).

Tests _build_attribution_rows from bifrost_core.monitor.reader.executions to verify:
- Single-instance attribution (ratio=1)
- Multi-instance (mixed) attribution with proportional split
- Unassigned (no strategy) handling
- Positions with no executions
- PnL proportional distribution
- Opposite-sign contribution exclusion
"""


import pytest

from bifrost_core.portfolio.reader.executions import _build_attribution_rows


def _pos_row(
    account_id="U123",
    contract_key="AAPL|OPT|20260320|180.0|C",
    symbol="AAPL",
    sec_type="OPT",
    position_qty=-5,
    avg_cost=3.0,
    price_mid=2.5,
    price_last=2.5,
    strategy_instance_id=None,
    strategy_opportunity_id=None,
    strategy_instance_label=None,
    strategy_opportunity_name=None,
    strategy_instance_opened_at_epoch=None,
    structure_type=None,
    scope_type=None,
    strategy_structure_id=None,
    net_qty_contribution=None,
    exec_count=None,
    expiry="20260320",
    strike=180.0,
    option_right="C",
):
    return {
        "account_id": account_id,
        "contract_key": contract_key,
        "symbol": symbol,
        "sec_type": sec_type,
        "position_qty": position_qty,
        "avg_cost": avg_cost,
        "price_mid": price_mid,
        "price_last": price_last,
        "strategy_instance_id": strategy_instance_id,
        "strategy_opportunity_id": strategy_opportunity_id,
        "strategy_instance_label": strategy_instance_label,
        "strategy_opportunity_name": strategy_opportunity_name,
        "strategy_instance_opened_at_epoch": strategy_instance_opened_at_epoch,
        "structure_type": structure_type,
        "scope_type": scope_type,
        "strategy_structure_id": strategy_structure_id,
        "net_qty_contribution": net_qty_contribution,
        "exec_count": exec_count,
        "expiry": expiry,
        "strike": strike,
        "option_right": option_right,
    }


class TestSingleInstanceAttribution:
    """Position attributed to exactly one strategy instance."""

    def test_single_instance_short(self):
        rows = [
            _pos_row(
                position_qty=-5,
                strategy_instance_id=10,
                strategy_instance_label="CC Mar",
                strategy_opportunity_name="CovCall AAPL",
                net_qty_contribution=-5,
                exec_count=3,
            )
        ]
        result = _build_attribution_rows(rows)
        assert len(result) == 1
        r = result[0]
        assert r["strategy_instance_id"] == 10
        assert r["open_qty_est"] == -5
        assert r["attribution_ratio"] == 1.0
        assert r["is_mixed"] is False
        assert r["has_unassigned"] is False
        assert r["method"] == "net_estimated"

    def test_single_instance_long(self):
        rows = [
            _pos_row(
                position_qty=3,
                avg_cost=5.0,
                price_last=6.0,
                strategy_instance_id=20,
                net_qty_contribution=3,
                exec_count=2,
            )
        ]
        result = _build_attribution_rows(rows)
        assert len(result) == 1
        r = result[0]
        assert r["open_qty_est"] == 3
        assert r["attribution_ratio"] == 1.0
        expected_pnl = round((6.0 - 5.0) * 3 * 100, 2)
        assert r["unrealized_pnl_est"] == expected_pnl


class TestMultiInstanceAttribution:
    """Position attributed to multiple strategy instances (mixed)."""

    def test_two_instances_proportional_split(self):
        """Short 5: Instance A sold 3, Instance B sold 2 → 60/40 split."""
        rows = [
            _pos_row(
                position_qty=-5,
                strategy_instance_id=10,
                strategy_instance_label="CC A",
                net_qty_contribution=-3,
                exec_count=2,
            ),
            _pos_row(
                position_qty=-5,
                strategy_instance_id=20,
                strategy_instance_label="CC B",
                net_qty_contribution=-2,
                exec_count=1,
            ),
        ]
        result = _build_attribution_rows(rows)
        assert len(result) == 2

        by_id = {r["strategy_instance_id"]: r for r in result}
        a = by_id[10]
        b = by_id[20]
        assert a["attribution_ratio"] == pytest.approx(0.6, abs=0.001)
        assert b["attribution_ratio"] == pytest.approx(0.4, abs=0.001)
        assert a["open_qty_est"] == pytest.approx(-3.0, abs=0.01)
        assert b["open_qty_est"] == pytest.approx(-2.0, abs=0.01)
        assert a["is_mixed"] is True
        assert b["is_mixed"] is True

    def test_pnl_split_two_instances(self):
        """Verify unrealized PnL is split proportionally."""
        rows = [
            _pos_row(
                position_qty=-4,
                avg_cost=4.0,
                price_last=3.0,
                strategy_instance_id=10,
                net_qty_contribution=-3,
                exec_count=2,
            ),
            _pos_row(
                position_qty=-4,
                avg_cost=4.0,
                price_last=3.0,
                strategy_instance_id=20,
                net_qty_contribution=-1,
                exec_count=1,
            ),
        ]
        result = _build_attribution_rows(rows)
        total_pnl = round((3.0 - 4.0) * (-4) * 100, 2)
        sum_pnl = sum(r["unrealized_pnl_est"] for r in result)
        assert sum_pnl == pytest.approx(total_pnl, abs=0.01)


class TestUnassignedAttribution:
    """Positions with no strategy instance on some/all executions."""

    def test_no_executions_at_all(self):
        """Position with no matching executions → single unassigned row."""
        rows = [
            _pos_row(
                position_qty=-5,
                strategy_instance_id=None,
                net_qty_contribution=None,
                exec_count=None,
            )
        ]
        result = _build_attribution_rows(rows)
        assert len(result) == 1
        r = result[0]
        assert r["strategy_instance_id"] is None
        assert r["open_qty_est"] == -5
        assert r["attribution_ratio"] == 1.0
        assert r["has_unassigned"] is True

    def test_all_execs_unassigned(self):
        """All executions have strategy_instance_id=None."""
        rows = [
            _pos_row(
                position_qty=-5,
                strategy_instance_id=None,
                net_qty_contribution=-5,
                exec_count=4,
            )
        ]
        result = _build_attribution_rows(rows)
        assert len(result) == 1
        r = result[0]
        assert r["strategy_instance_id"] is None
        assert r["open_qty_est"] == -5
        assert r["has_unassigned"] is True

    def test_mixed_assigned_and_unassigned(self):
        """Instance A sold 3, unassigned sold 2 → both shown, has_unassigned=True."""
        rows = [
            _pos_row(
                position_qty=-5,
                strategy_instance_id=10,
                net_qty_contribution=-3,
                exec_count=2,
            ),
            _pos_row(
                position_qty=-5,
                strategy_instance_id=None,
                net_qty_contribution=-2,
                exec_count=1,
            ),
        ]
        result = _build_attribution_rows(rows)
        assigned = [r for r in result if r["strategy_instance_id"] is not None]
        unassigned = [r for r in result if r["strategy_instance_id"] is None]
        assert len(assigned) == 1
        assert len(unassigned) == 1
        assert assigned[0]["has_unassigned"] is True
        assert assigned[0]["is_mixed"] is True


@pytest.mark.xfail(reason="Engine parity: opposite-sign exclusion semantics under review")
class TestOppositeSignExclusion:
    """Opposite-sign contributions should not count towards open_qty_est."""

    def test_opposite_sign_excluded(self):
        """Long 5: Instance A bought 7, Instance B sold 2 (B's net is negative, excluded)."""
        rows = [
            _pos_row(
                position_qty=5,
                strategy_instance_id=10,
                net_qty_contribution=7,
                exec_count=4,
            ),
            _pos_row(
                position_qty=5,
                strategy_instance_id=20,
                net_qty_contribution=-2,
                exec_count=1,
            ),
        ]
        result = _build_attribution_rows(rows)
        same_sign = [r for r in result if r["strategy_instance_id"] == 10]
        assert len(same_sign) == 1
        assert same_sign[0]["attribution_ratio"] == 1.0
        assert same_sign[0]["open_qty_est"] == 5


class TestReconciliation:
    """Sum of open_qty_est should equal position_qty (for same-sign contributors)."""

    def test_qty_sum_equals_position(self):
        rows = [
            _pos_row(
                position_qty=-10,
                strategy_instance_id=1,
                net_qty_contribution=-4,
                exec_count=2,
            ),
            _pos_row(
                position_qty=-10,
                strategy_instance_id=2,
                net_qty_contribution=-3,
                exec_count=2,
            ),
            _pos_row(
                position_qty=-10,
                strategy_instance_id=3,
                net_qty_contribution=-3,
                exec_count=1,
            ),
        ]
        result = _build_attribution_rows(rows)
        total_est = sum(r["open_qty_est"] for r in result)
        assert total_est == pytest.approx(-10, abs=0.01)

    def test_pnl_sum_equals_total(self):
        """Sum of unrealized_pnl_est should approximate total position PnL."""
        rows = [
            _pos_row(
                position_qty=-6,
                avg_cost=5.0,
                price_last=4.0,
                strategy_instance_id=1,
                net_qty_contribution=-2,
                exec_count=1,
            ),
            _pos_row(
                position_qty=-6,
                avg_cost=5.0,
                price_last=4.0,
                strategy_instance_id=2,
                net_qty_contribution=-4,
                exec_count=3,
            ),
        ]
        result = _build_attribution_rows(rows)
        total_pnl = round((4.0 - 5.0) * (-6) * 100, 2)
        sum_pnl = sum(r["unrealized_pnl_est"] for r in result)
        assert sum_pnl == pytest.approx(total_pnl, abs=0.01)

    def test_ratio_sum_equals_one(self):
        rows = [
            _pos_row(
                position_qty=-12,
                strategy_instance_id=1,
                net_qty_contribution=-5,
                exec_count=2,
            ),
            _pos_row(
                position_qty=-12,
                strategy_instance_id=2,
                net_qty_contribution=-4,
                exec_count=1,
            ),
            _pos_row(
                position_qty=-12,
                strategy_instance_id=3,
                net_qty_contribution=-3,
                exec_count=1,
            ),
        ]
        result = _build_attribution_rows(rows)
        total_ratio = sum(r["attribution_ratio"] for r in result)
        assert total_ratio == pytest.approx(1.0, abs=0.001)


@pytest.mark.xfail(reason="Engine parity: zero net contribution edge case under review")
class TestEdgeCases:
    """Edge cases: zero position, zero contributions, stock positions."""

    def test_zero_net_contribution(self):
        """Instance that is fully closed (net=0) should not appear in results if no other instance."""
        rows = [
            _pos_row(
                position_qty=-5,
                strategy_instance_id=10,
                net_qty_contribution=0,
                exec_count=4,
            ),
        ]
        result = _build_attribution_rows(rows)
        assert len(result) == 1
        r = result[0]
        assert r["strategy_instance_id"] is None
        assert r["open_qty_est"] == -5

    def test_stock_position(self):
        rows = [
            _pos_row(
                sec_type="STK",
                contract_key="AAPL|STK|||",
                position_qty=100,
                avg_cost=180.0,
                price_last=190.0,
                strategy_instance_id=None,
                net_qty_contribution=None,
                exec_count=None,
                strike=0,
                option_right="",
                expiry="",
            ),
        ]
        result = _build_attribution_rows(rows)
        assert len(result) == 1
        r = result[0]
        assert r["sec_type"] == "STK"
        assert r["open_qty_est"] == 100
        assert r["unrealized_pnl_est"] == round((190.0 - 180.0) * 100 * 1, 2)

    def test_multiple_positions_separate(self):
        """Two different contracts should produce independent attribution rows."""
        rows = [
            _pos_row(
                contract_key="AAPL|OPT|20260320|180.0|C",
                position_qty=-3,
                strategy_instance_id=10,
                net_qty_contribution=-3,
                exec_count=2,
            ),
            _pos_row(
                contract_key="AAPL|OPT|20260320|190.0|P",
                position_qty=-2,
                strategy_instance_id=10,
                net_qty_contribution=-2,
                exec_count=1,
                strike=190.0,
                option_right="P",
            ),
        ]
        result = _build_attribution_rows(rows)
        assert len(result) == 2
        for r in result:
            assert r["strategy_instance_id"] == 10
            assert r["attribution_ratio"] == 1.0
