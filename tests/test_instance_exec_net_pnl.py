"""TD-48: execution-book Net PnL per strategy instance had no test.

`compute_instance_exec_derived_net_pnl` mirrors the frontend's
computeInstanceExecDerivedNetPnl: OPT premium groups (x100, commission in), non-OPT
realized PnL from the book, plus option-stock link slippage prorated to the
instance's slice. The reads are replaced; executions, ids and accounts are made up.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from bifrost_core.portfolio.reader import instance_exec_net_pnl as m

ACCOUNT = "U0000001"
CALL = "ZZZ|OPT|20261120|C|50"
PUT = "ZZZ|OPT|20261120|P|45"


def _executions() -> List[Dict[str, Any]]:
    return [
        # whole-execution attribution to instance 11
        {
            "account_executions_id": 101, "account_id": ACCOUNT, "sec_type": "OPT",
            "contract_key": CALL, "strike": 50, "side": "SELL", "quantity": 2,
            "price": 1.50, "commission": 1.30, "strategy_instance_id": 11,
        },
        {
            # strike as text: must land in the same group as 50 (Decimal / str splits)
            "account_executions_id": 102, "account_id": ACCOUNT, "sec_type": "OPT",
            "contract_key": CALL, "strike": "50.0", "side": "BOT", "quantity": 2,
            "price": 0.40, "commission": 1.30, "strategy_instance_id": 11,
        },
        {
            "account_executions_id": 104, "account_id": ACCOUNT, "sec_type": "STK",
            "contract_key": "ZZZ|STK|||", "side": "SELL", "quantity": 100,
            "price": 51.0, "realized_pnl": 25.0, "strategy_instance_id": 11,
        },
        # split execution: a quarter of it belongs to instance 11
        {
            "account_executions_id": 103, "account_id": ACCOUNT, "sec_type": "OPT",
            "contract_key": PUT, "strike": 45, "side": "SLD", "quantity": 4,
            "price": 1.00, "commission": 2.0,
            "instance_allocations": [
                {"strategy_instance_id": 11, "allocated_quantity": 1},
                {"strategy_instance_id": 12, "allocated_quantity": 3},
            ],
        },
        # another instance's fill: ignored
        {
            "account_executions_id": 105, "account_id": ACCOUNT, "sec_type": "OPT",
            "contract_key": CALL, "strike": 50, "side": "BUY", "quantity": 9,
            "price": 9.0, "commission": 9.0, "strategy_instance_id": 12,
        },
    ]


LINKS = {
    "by_option_id": {
        "101": {"slippage_total": -6.0},
        "103": {"links": [{"slippage_vs_close": 4.0}, {"slippage_vs_close": None}]},
    }
}


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    seen: Dict[str, Any] = {"links": LINKS}

    def fake_get_executions(conn: Any, **kw: Any) -> List[Dict[str, Any]]:
        seen["executions_kw"] = kw
        return _executions()

    def fake_links(conn: Any, batches: Any) -> Any:
        seen["batches"] = batches
        if isinstance(seen["links"], Exception):
            raise seen["links"]
        return seen["links"]

    monkeypatch.setattr(m, "get_executions", fake_get_executions)
    monkeypatch.setattr(m, "get_option_stock_links_bulk", fake_links)
    return seen


def test_net_pnl_adds_opt_groups_book_pnl_and_prorated_slippage(reads: Dict[str, Any]) -> None:
    # CALL group: sell 1.50*2*100 - 1.30 = 298.70; buy 0.40*2*100 + 1.30 = 81.30 -> 217.40
    # PUT slice (1 of 4): sell 1.00*1*100 - 0.50 = 99.50
    # STK realized 25.00; slippage -6.00 * (2/2) + 4.00 * (1/4) = -5.00
    total = m.compute_instance_exec_derived_net_pnl(object(), 11, since_ts=1.0, until_ts=2.0)
    assert total == 336.90
    kw = reads["executions_kw"]
    assert kw["strategy_instance_id"] == 11 and kw["source_scope"] == "performance_book"
    assert (kw["since_ts"], kw["until_ts"], kw["limit"]) == (1.0, 2.0, 50000)
    assert reads["batches"] == [(ACCOUNT, [101, 102, 103])]


def test_slippage_read_failure_counts_as_zero(reads: Dict[str, Any]) -> None:
    reads["links"] = RuntimeError("link read failed")
    assert m.compute_instance_exec_derived_net_pnl(object(), 11) == 341.90


def test_no_connection_bad_id_or_no_fills_is_zero(reads: Dict[str, Any]) -> None:
    assert m.compute_instance_exec_derived_net_pnl(None, 11) == 0.0
    assert m.compute_instance_exec_derived_net_pnl(object(), "eleven") == 0.0
    assert m.compute_instance_exec_derived_net_pnl(object(), 99) == 0.0


def test_slice_prorates_a_split_execution() -> None:
    ex = _executions()[3] | {
        "realized_pnl": 8.0, "taxes": 0.4, "net_cash": 396.0,
        "strategy_opportunity_id": 7, "strategy_opportunity_name": " Fade ",
    }
    ex["instance_allocations"][0]["strategy_instance_label"] = " #11 "
    out = m.slice_execution_for_instance_opt_view(ex, 11)
    assert out is not None
    assert out["quantity"] == 1
    assert out["commission"] == 0.5 and out["realized_pnl"] == 2.0
    assert out["taxes"] == pytest.approx(0.1) and out["net_cash"] == 99.0
    assert out["strategy_instance_id"] == 11 and out["strategy_instance_label"] == "#11"
    assert out["strategy_opportunity_id"] == 7 and out["strategy_opportunity_name"] == "Fade"
    assert out["instance_allocations"] is None
    assert ex["quantity"] == 4  # the input is not modified


def test_slice_allocation_opportunity_overrides_parent_id() -> None:
    ex = _executions()[3] | {"strategy_opportunity_id": 7, "strategy_opportunity_name": "Fade"}
    ex["instance_allocations"][0]["strategy_opportunity_id"] = 8
    out = m.slice_execution_for_instance_opt_view(ex, 11)
    assert out["strategy_opportunity_id"] == 8
    # Pinned as it is today, and wrong: the parent's name ("Fade", opportunity 7) stays on
    # a slice that now says opportunity 8. The frontend's sliceExecutionForInstanceOptView
    # (ledgerOptHelpers.ts) answers null here; core clears the name only when the parent
    # has no opportunity. The PnL number is unaffected. Not changed in this tests-only
    # batch; reported.
    assert out["strategy_opportunity_name"] == "Fade"
    no_parent = _executions()[3]
    no_parent["instance_allocations"][0]["strategy_opportunity_id"] = 8
    assert m.slice_execution_for_instance_opt_view(no_parent, 11)["strategy_opportunity_name"] is None


@pytest.mark.parametrize(
    "ex,instance",
    [
        (_executions()[3], 13),  # not among the allocations
        ({"instance_allocations": [{"strategy_instance_id": 11, "allocated_quantity": 0}]}, 11),
        (_executions()[0], 12),  # whole execution, other instance
        (_executions()[0], "x"),
    ],
)
def test_slice_is_none_when_the_execution_is_not_the_instances(ex: Dict[str, Any], instance: Any) -> None:
    assert m.slice_execution_for_instance_opt_view(ex, instance) is None


def test_slippage_entry_prefers_total_and_sums_links_otherwise() -> None:
    assert m._slippage_usd_from_link_entry({"slippage_total": 3.5, "links": [{"slippage_vs_close": 9}]}) == 3.5
    assert m._slippage_usd_from_link_entry({"slippage_total": "nan", "links": [{"slippage_vs_close": 2}]}) == 2.0
    assert m._slippage_usd_from_link_entry({}) == 0.0
