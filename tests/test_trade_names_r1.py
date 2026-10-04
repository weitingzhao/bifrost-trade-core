"""Naming program R1 (core 0.42.0): every instance key is read and written under its trade name too.

Rows carry the new key beside the old one with the same value; writers take the new
name and, when both are sent, the new one wins. Accounts, ids and labels are made up.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from bifrost_core.monitor.reader import trade_names as tn
from bifrost_core.monitor.reader import trade_review
from bifrost_core.monitor.schemas.strategy_plans import PlanLinkFillBody
from bifrost_core.portfolio.reader import accounts
from bifrost_core.portfolio.reader import executions as executions_reader
from bifrost_core.portfolio.reader.accounts_helpers import _rows_to_executions
from test_signed_qty import NEW_BOOK, _perf
from test_write_portfolio import ACCOUNT, CFG, _env, _golden, two_dbs  # noqa: F401 (fixture)
from write_fakes import FakeConn, Reply

SPLIT = {
    "strategy_instance_id": 41,
    "allocated_quantity": -2.0,
    "strategy_opportunity_id": 5,
    "strategy_instance_label": "#041",
}


def test_a_row_carries_both_names() -> None:
    row: Dict[str, Any] = {
        "strategy_instance_id": 41,
        "strategy_instance_label": "#041",
        "strategy_instance_opened_at_epoch": 1_800_000_000,
        "instance_allocations": [SPLIT],
    }
    tn.add_trade_names(row)
    assert row["trade_id"] == row["strategy_instance_id"] == 41
    assert row["trade_label"] == "#041"
    assert row["trade_opened_at_epoch"] == 1_800_000_000
    assert row["instance_allocations"] == [SPLIT]  # the old shape is untouched
    assert row["fill_splits"] == [{"trade_id": 41, "quantity": -2.0, "strategy_opportunity_id": 5, "trade_label": "#041"}]


def test_a_row_without_instance_keys_gains_nothing() -> None:
    row = {"account_id": ACCOUNT, "symbol": "ZZQ"}
    assert tn.add_trade_names(dict(row)) == row


def test_an_unattributed_fill_reads_null_under_both_names() -> None:
    row = tn.add_trade_names({"strategy_instance_id": None})
    assert row == {"strategy_instance_id": None, "trade_id": None}


def test_execution_rows_carry_trade_names() -> None:
    rows = _rows_to_executions(
        [{"account_executions_id": 7, "strategy_instance_id": 41, "strategy_instance_label": "#041",
          "strategy_instance_opened_at_epoch": 1_800_000_000, "time": 1.0}],
        None,
    )
    assert rows[0]["trade_id"] == 41 and rows[0]["trade_label"] == "#041"
    assert rows[0]["trade_opened_at_epoch"] == 1_800_000_000


def test_attached_splits_come_as_fill_splits_too() -> None:
    conn = FakeConn([("FROM brokerage.trade_fill_splits", Reply(all=[{"account_executions_id": 7, **SPLIT}]))])
    ex = [{"account_executions_id": 7}, {"account_executions_id": 8}]
    executions_reader.attach_instance_allocations(conn, ex)
    assert ex[0]["instance_allocations"][0]["strategy_instance_id"] == 41
    assert ex[0]["fill_splits"] == [{"trade_id": 41, "quantity": -2.0, "strategy_opportunity_id": 5, "trade_label": "#041"}]
    assert "fill_splits" not in ex[1]


@pytest.mark.parametrize("kw", [{}, {"strategy_instance_id": 11}])
def test_performance_names_the_trade_breakdown(monkeypatch: pytest.MonkeyPatch, kw: Dict[str, Any]) -> None:
    perf = _perf(monkeypatch, NEW_BOOK, **kw)
    old, new = perf["realized_by_strategy_instance"], perf["realized_by_trade"]
    assert old and len(new) == len(old)
    for o, n in zip(old, new):
        assert n == {**o, "trade_id": o["strategy_instance_id"]}


def test_summary_only_has_an_empty_trade_breakdown() -> None:
    out = executions_reader._performance_response_summary_only(
        fill_count=0, total_realized_pnl=0.0, total_commission=0.0, net_pnl=0.0, win_count=0, loss_count=0
    )
    assert out["realized_by_trade"] == [] == out["realized_by_strategy_instance"]


# --- writers ----------------------------------------------------------------------------


def test_writer_fields_take_the_new_names() -> None:
    sent = {"trade_id": 41, "fill_splits": [{"trade_id": 41, "quantity": 1}, {"trade_id": 42, "quantity": 1}]}
    assert tn.fields_as_instance(sent) == {
        "strategy_instance_id": 41,
        "instance_allocations": [
            {"strategy_instance_id": 41, "allocated_quantity": 1},
            {"strategy_instance_id": 42, "allocated_quantity": 1},
        ],
    }
    assert sent == {"trade_id": 41, "fill_splits": [{"trade_id": 41, "quantity": 1}, {"trade_id": 42, "quantity": 1}]}


def test_the_new_name_wins_when_both_are_sent() -> None:
    out = tn.fields_as_instance(
        {"strategy_instance_id": 9, "trade_id": 41, "instance_allocations": [SPLIT], "fill_splits": []}
    )
    assert out == {"strategy_instance_id": 41, "instance_allocations": []}


def test_old_names_still_pass_through() -> None:
    sent = {"strategy_instance_id": 41, "instance_allocations": [{"strategy_instance_id": 41, "allocated_quantity": 2}]}
    assert tn.fields_as_instance(sent) == sent


def test_patch_execution_takes_trade_id(two_dbs) -> None:  # noqa: F811
    env, golden = _env(), _golden()
    two_dbs(env, golden)
    out = accounts.patch_execution(CFG, 77, {"strategy_opportunity_id": 5, "trade_id": 41})
    assert out["trade_id"] == out["strategy_instance_id"] == 41
    assert out["fill_splits"] == out["instance_allocations"] == []
    _, params = env.statement("INSERT INTO trade_execution")
    assert params == (ACCOUNT, "0000e1.01", 41)


def test_patch_execution_refuses_a_bad_split_under_its_new_name(two_dbs) -> None:  # noqa: F811
    two_dbs(_env(), _golden())
    with pytest.raises(accounts.WriteInvalid, match=r"fill_splits must be a list of \{trade_id, quantity\}"):
        accounts.patch_execution(CFG, 77, {"fill_splits": [7]})


def test_review_rows_and_writes_take_the_json_names() -> None:
    row = trade_review._row_out({"strategy_instance_id": 41, "tags_added": '["a"]', "tags_dropped": [], "reviewed_at": None})
    assert row["tags_added_json"] == row["tags_added"] == ["a"]
    assert row["tags_dropped_json"] == [] and row["trade_id"] == 41
    assert tn.review_fields_as_columns({"tags_added_json": ["b"], "tags_added": ["c"], "reviewed": True}) == {
        "tags_added": ["b"],
        "reviewed": True,
    }


def test_review_patch_takes_tags_added_json() -> None:
    returned = {"trade_review_id": 1, "strategy_instance_id": 41, "tags_added": ["b"], "tags_dropped": [],
                "reviewed_at": None, "created_at": None, "updated_at": None}
    conn = FakeConn([("SELECT 1 FROM trade WHERE trade_id", Reply(one=(1,))), ("INSERT INTO trade_review", Reply(one=returned))])
    out = trade_review.patch_review(conn, 41, {"tags_added_json": ["b"]})
    sql, params = conn.statement("INSERT INTO trade_review")
    assert "tags_added" in sql and params["tags_added"] == '["b"]'
    assert out["tags_added_json"] == ["b"] and out["trade_id"] == 41


def test_link_fill_body_takes_either_name() -> None:
    assert PlanLinkFillBody(trade_id=41).strategy_instance_id == 41
    assert PlanLinkFillBody(strategy_instance_id=41).trade_id == 41
    assert PlanLinkFillBody(trade_id=41, strategy_instance_id=9).strategy_instance_id == 41
    with pytest.raises(ValidationError, match="trade_id is required"):
        PlanLinkFillBody()


def test_split_items_are_copied_not_mutated() -> None:
    item = copy.deepcopy(SPLIT)
    tn.add_trade_names({"instance_allocations": [item]})
    assert item == SPLIT
