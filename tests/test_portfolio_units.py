"""The contract-unit boundary, pinned.

These are the conversions whose absence produced a 100x error that read as a
confident dollar figure on two pages.
"""

import pytest

from bifrost_core.portfolio.units import (
    OPTION_MULTIPLIER,
    is_option,
    option_cost_per_share,
    position_value,
)


class TestOptionCostPerShare:
    def test_option_cost_is_divided_by_the_multiplier(self):
        # IB reported 8415.776 for one DAVE 280 call: 84.15776 a share.
        assert option_cost_per_share(8415.776, "OPT") == pytest.approx(84.15776)

    def test_stock_cost_passes_through(self):
        assert option_cost_per_share(182.38, "STK") == pytest.approx(182.38)

    def test_sec_type_matching_is_forgiving_about_case_and_padding(self):
        assert option_cost_per_share(100.0, " opt ") == pytest.approx(1.0)
        assert is_option("Opt") is True
        assert is_option("STK") is False
        assert is_option(None) is False

    def test_unparseable_returns_none_not_zero(self):
        # A zero cost basis turns the whole position into profit.
        assert option_cost_per_share(None, "OPT") is None
        assert option_cost_per_share("abc", "OPT") is None
        assert option_cost_per_share(float("nan"), "OPT") is None
        assert option_cost_per_share(float("inf"), "OPT") is None

    def test_zero_cost_is_preserved_when_it_is_genuinely_zero(self):
        assert option_cost_per_share(0.0, "OPT") == 0.0


class TestPositionValue:
    def test_option_scales_by_the_multiplier(self):
        assert position_value(2.5, -3, "OPT") == pytest.approx(-750.0)

    def test_stock_does_not(self):
        assert position_value(2.5, -3, "STK") == pytest.approx(-7.5)

    def test_missing_inputs_return_none(self):
        assert position_value(None, 1, "OPT") is None
        assert position_value(1.0, None, "OPT") is None
        assert position_value(float("nan"), 1, "OPT") is None

    def test_the_pairing_that_used_to_be_wrong(self):
        # (per-share price - per-CONTRACT cost) * qty * 100 was the defect.
        price_per_share = 63.03
        ib_avg_cost_per_contract = 8415.776
        qty = -1

        cost_per_share = option_cost_per_share(ib_avg_cost_per_contract, "OPT")
        correct = position_value(price_per_share - cost_per_share, qty, "OPT")
        assert correct == pytest.approx(2112.776, abs=0.01)

        broken = (price_per_share - ib_avg_cost_per_contract) * qty * OPTION_MULTIPLIER
        assert broken == pytest.approx(835274.6, abs=1.0)
