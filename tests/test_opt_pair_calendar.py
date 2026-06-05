"""Tests for OPT pair FIFO and _compute_opt_realized_calendar alignment."""

import math
from bifrost_core.portfolio.reader.accounts_helpers import (
    _compute_opt_pair_map_and_pairs,
    _compute_opt_realized_calendar,
)


def _make_exec(
    eid: int,
    symbol: str = "AAPL",
    expiry: str = "20260320",
    strike: str = "200",
    account_id: str = "U123",
    side: str = "BUY",
    quantity: float = 1.0,
    price: float = 5.0,
    commission: float = 1.0,
    time: float = 1742900000.0,
    trade_date: str = "2025-03-25",
) -> dict:
    return {
        "account_executions_id": eid,
        "symbol": symbol,
        "expiry": expiry,
        "strike": strike,
        "account_id": account_id,
        "sec_type": "OPT",
        "side": side,
        "quantity": quantity,
        "price": price,
        "commission": commission,
        "time": time,
        "trade_date": trade_date,
    }


class TestFIFOPairQuantity:
    """pair['quantity'] must equal the matched amount, not the full execution quantity."""

    def test_full_match(self):
        execs = [
            _make_exec(1, side="BUY", quantity=2, price=3.0, time=100),
            _make_exec(2, side="SELL", quantity=2, price=4.0, time=200),
        ]
        _, pairs = _compute_opt_pair_map_and_pairs(execs)
        assert len(pairs) == 1
        assert pairs[0]["quantity"] == 2.0
        assert pairs[0]["leg_c_execution_id"] == 1
        assert pairs[0]["leg_p_execution_id"] == 2

    def test_partial_match_leaves_remainder(self):
        execs = [
            _make_exec(1, side="BUY", quantity=5, price=3.0, time=100),
            _make_exec(2, side="SELL", quantity=2, price=4.0, time=200),
        ]
        _, pairs = _compute_opt_pair_map_and_pairs(execs)
        assert len(pairs) == 1
        assert pairs[0]["quantity"] == 2.0

    def test_multiple_sells_against_one_buy(self):
        execs = [
            _make_exec(1, side="BUY", quantity=5, price=3.0, time=100),
            _make_exec(2, side="SELL", quantity=2, price=4.0, time=200),
            _make_exec(3, side="SELL", quantity=3, price=5.0, time=300),
        ]
        _, pairs = _compute_opt_pair_map_and_pairs(execs)
        assert len(pairs) == 2
        assert pairs[0]["quantity"] == 2.0
        assert pairs[1]["quantity"] == 3.0

    def test_no_match_no_pairs(self):
        execs = [
            _make_exec(1, side="BUY", quantity=3, price=3.0, time=100),
        ]
        _, pairs = _compute_opt_pair_map_and_pairs(execs)
        assert len(pairs) == 0


class TestOptRealizedCalendarUsesFIFO:
    """_compute_opt_realized_calendar should use the same FIFO as _compute_opt_pair_map_and_pairs."""

    def test_same_day_pair_attribution(self):
        execs = [
            _make_exec(1, side="BUY", quantity=1, price=3.0, time=100, trade_date="2025-03-25"),
            _make_exec(2, side="SELL", quantity=1, price=4.0, time=200, trade_date="2025-03-25"),
        ]
        calendar = _compute_opt_realized_calendar(execs, "day")
        assert len(calendar) == 1
        assert calendar[0]["trade_count"] == 1
        assert calendar[0]["sec_type"] == "OPT"

    def test_cross_day_pair_attributed_to_later_leg(self):
        execs = [
            _make_exec(1, side="BUY", quantity=1, price=3.0, time=100, trade_date="2025-03-24"),
            _make_exec(2, side="SELL", quantity=1, price=4.0, time=200, trade_date="2025-03-25"),
        ]
        calendar = _compute_opt_realized_calendar(execs, "day")
        assert len(calendar) == 1
        assert calendar[0]["period_label"] == "2025-03-25"

    def test_partial_match_only_matched_amount_realized(self):
        """BUY 5 + SELL 2 → 1 pair (qty=2), remaining 3 is unrealized (not in calendar)."""
        execs = [
            _make_exec(1, side="BUY", quantity=5, price=3.0, time=100),
            _make_exec(2, side="SELL", quantity=2, price=4.0, time=200),
        ]
        calendar = _compute_opt_realized_calendar(execs, "day")
        assert len(calendar) == 1
        assert calendar[0]["trade_count"] == 1
        expected_net = (-2 * 3.0 * 100 - 0.4) + (2 * 4.0 * 100 - 1.0)
        assert abs(calendar[0]["net_pnl"] - round(expected_net, 2)) < 0.02

    def test_no_match_empty_calendar(self):
        execs = [
            _make_exec(1, side="BUY", quantity=3, price=3.0, time=100),
        ]
        calendar = _compute_opt_realized_calendar(execs, "day")
        assert len(calendar) == 0


class TestPartialMatchScenario:
    """Same contract has both paired and unpaired executions on the same day."""

    def test_same_contract_partial_pair(self):
        execs = [
            _make_exec(1, side="BUY", quantity=5, price=3.0, time=100, commission=1.0),
            _make_exec(2, side="SELL", quantity=2, price=4.0, time=200, commission=0.5),
            _make_exec(3, side="BUY", quantity=3, price=2.5, time=300, commission=0.8),
        ]
        pair_map, pairs = _compute_opt_pair_map_and_pairs(execs)

        assert len(pairs) == 1
        assert pairs[0]["quantity"] == 2.0

        paired_ids = set()
        for p in pairs:
            paired_ids.add(p["leg_c_execution_id"])
            paired_ids.add(p["leg_p_execution_id"])
        assert 1 in paired_ids
        assert 2 in paired_ids

        assert 3 not in paired_ids


class TestIterativeFIFOEquivalence:
    """Iterative one-pair-per-round FIFO produces the same results as a single-pass."""

    def test_buy_split_across_two_sells(self):
        """BUY(5) matched by SELL(2) then SELL(3): two pairs consuming the BUY entirely."""
        execs = [
            _make_exec(1, side="BUY", quantity=5, price=3.0, time=100, commission=1.0),
            _make_exec(2, side="SELL", quantity=2, price=4.0, time=200, commission=0.5),
            _make_exec(3, side="SELL", quantity=3, price=5.0, time=300, commission=0.8),
        ]
        _, pairs = _compute_opt_pair_map_and_pairs(execs)
        assert len(pairs) == 2
        assert pairs[0]["quantity"] == 2.0
        assert pairs[0]["leg_c_execution_id"] == 1
        assert pairs[0]["leg_p_execution_id"] == 2
        assert pairs[1]["quantity"] == 3.0
        assert pairs[1]["leg_c_execution_id"] == 1
        assert pairs[1]["leg_p_execution_id"] == 3
        total_comm = sum(p["commission"] for p in pairs)
        assert abs(total_comm - (1.0 + 0.5 + 0.8)) < 0.02

    def test_interleaved_buys_and_sells(self):
        """BUY(5), SELL(2), BUY(1), SELL(4): three pairs, correct FIFO order."""
        execs = [
            _make_exec(1, side="BUY", quantity=5, price=3.0, time=100, commission=1.0),
            _make_exec(2, side="SELL", quantity=2, price=4.0, time=200, commission=0.6),
            _make_exec(3, side="BUY", quantity=1, price=2.0, time=250, commission=0.3),
            _make_exec(4, side="SELL", quantity=4, price=5.0, time=300, commission=1.2),
        ]
        _, pairs = _compute_opt_pair_map_and_pairs(execs)
        assert len(pairs) == 3
        assert pairs[0]["quantity"] == 2.0
        assert (pairs[0]["leg_c_execution_id"], pairs[0]["leg_p_execution_id"]) == (1, 2)
        assert pairs[1]["quantity"] == 3.0
        assert (pairs[1]["leg_c_execution_id"], pairs[1]["leg_p_execution_id"]) == (1, 4)
        assert pairs[2]["quantity"] == 1.0
        assert (pairs[2]["leg_c_execution_id"], pairs[2]["leg_p_execution_id"]) == (3, 4)

    def test_partial_match_leaves_exec_for_unrealized(self):
        """BUY(5) + SELL(2): the BUY appears in pair but still has qty 3 unmatched.

        matchedQtyById must account for partial consumption, not binary presence.
        """
        execs = [
            _make_exec(1, side="BUY", quantity=5, price=3.0, time=100, commission=1.0),
            _make_exec(2, side="SELL", quantity=2, price=4.0, time=200, commission=0.5),
        ]
        _, pairs = _compute_opt_pair_map_and_pairs(execs)
        assert len(pairs) == 1
        assert pairs[0]["quantity"] == 2.0

        matched_qty = {}
        for p in pairs:
            for leg_key in ("leg_c_execution_id", "leg_p_execution_id"):
                eid = p[leg_key]
                matched_qty[eid] = matched_qty.get(eid, 0) + p["quantity"]

        assert matched_qty.get(1) == 2.0
        unmatched_buy = 5.0 - matched_qty.get(1, 0)
        assert abs(unmatched_buy - 3.0) < 1e-9

    def test_commission_allocation_across_rounds(self):
        """Commission must be proportionally split across iterative pair rounds."""
        execs = [
            _make_exec(1, side="BUY", quantity=4, price=3.0, time=100, commission=2.0),
            _make_exec(2, side="SELL", quantity=1, price=4.0, time=200, commission=0.5),
            _make_exec(3, side="SELL", quantity=3, price=5.0, time=300, commission=1.5),
        ]
        _, pairs = _compute_opt_pair_map_and_pairs(execs)
        assert len(pairs) == 2
        assert pairs[0]["quantity"] == 1.0
        assert pairs[0]["commission"] == round((1 / 4) * 2.0 + 0.5, 2)
        assert pairs[1]["quantity"] == 3.0
        assert pairs[1]["commission"] == round((3 / 3) * (2.0 - (1 / 4) * 2.0) + 1.5, 2)

    def test_same_time_tiebreaker_lower_id_first(self):
        """Same calendar day + same time: lower account_executions_id is earlier in FIFO (matches frontend)."""
        execs = [
            _make_exec(164, side="BUY", quantity=4, price=1.0, time=100, trade_date="2025-03-25"),
            _make_exec(163, side="BUY", quantity=1, price=1.0, time=100, trade_date="2025-03-25"),
            _make_exec(160, side="SELL", quantity=1, price=1.0, time=200, trade_date="2025-03-25"),
        ]
        _, pairs = _compute_opt_pair_map_and_pairs(execs)
        assert len(pairs) == 1
        assert pairs[0]["quantity"] == 1.0
        assert pairs[0]["leg_c_execution_id"] == 163
        assert pairs[0]["leg_p_execution_id"] == 160
