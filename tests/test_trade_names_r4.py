"""Naming program R4 (core 0.47.0): the Trade's names only.

R1 (core 0.42.0) put the trade names beside the instance names on every reader row and let
writers take either; R4 drops the instance names. Rows carry ``trade_id``, ``trade_label``,
``trade_opened_at_epoch``, ``fill_splits [{trade_id, quantity, ...}]``, ``realized_by_trade``
and ``tags_*_json`` only; a writer sent an old name refuses it (unknown key) or ignores it.
Accounts, ids and labels are made up.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from pydantic import ValidationError

from bifrost_core.monitor.reader import common as common_reader
from bifrost_core.monitor.reader import trade_review
from bifrost_core.monitor.schemas.strategy_plans import PlanLinkFillBody
from bifrost_core.persistence.postgres import brokerage_tables, brokerage_views, trade_ddl
from bifrost_core.portfolio.reader import accounts
from bifrost_core.portfolio.reader import executions as executions_reader
from bifrost_core.portfolio.reader.accounts_helpers import _rows_to_executions
from test_signed_qty import NEW_BOOK, _perf
from test_write_portfolio import ACCOUNT, CFG, _env, _golden, two_dbs  # noqa: F401 (fixture)
from write_fakes import FakeConn, Reply

OLD_KEYS = (
    "strategy_instance_id",
    "strategy_instance_label",
    "strategy_instance_opened_at_epoch",
    "instance_allocations",
    "realized_by_strategy_instance",
    "allocated_quantity",
)

SPLIT_ROW = {"trade_id": 41, "quantity": -2.0, "strategy_opportunity_id": 5, "trade_label": "#041"}


def _no_old_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in OLD_KEYS, k
            _no_old_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            _no_old_keys(v)


def test_execution_rows_carry_the_trade_names_only() -> None:
    rows = _rows_to_executions(
        [{"account_executions_id": 7, "trade_id": 41, "trade_label": "#041",
          "trade_opened_at_epoch": 1_800_000_000, "time": 1.0}],
        None,
    )
    assert rows[0]["trade_id"] == 41 and rows[0]["trade_label"] == "#041"
    assert rows[0]["trade_opened_at_epoch"] == 1_800_000_000
    _no_old_keys(rows)


def test_attached_splits_are_fill_splits_only() -> None:
    conn = FakeConn([("FROM brokerage.trade_fill_splits", Reply(all=[{"account_executions_id": 7, **SPLIT_ROW}]))])
    ex = [{"account_executions_id": 7}, {"account_executions_id": 8}]
    executions_reader.attach_fill_splits(conn, ex)
    assert ex[0]["fill_splits"] == [SPLIT_ROW]
    assert "fill_splits" not in ex[1]
    _no_old_keys(ex)
    sql, _ = conn.statement("FROM brokerage.trade_fill_splits")
    assert "AS strategy_instance" not in sql and "allocated_quantity" not in sql


def test_a_split_weighs_its_trade() -> None:
    ex = {"fill_splits": [{"trade_id": 41, "quantity": 3}, {"trade_id": 42, "quantity": 1}]}
    assert executions_reader.weight_realized_for_trade(ex, 41) == 0.75
    assert executions_reader.weight_realized_for_trade({"trade_id": 9}, 9) == 1.0


@pytest.mark.parametrize("kw", [{}, {"trade_id": 11}])
def test_performance_has_realized_by_trade_only(monkeypatch: pytest.MonkeyPatch, kw: Dict[str, Any]) -> None:
    perf = _perf(monkeypatch, NEW_BOOK, **kw)
    assert "realized_by_strategy_instance" not in perf
    assert all("trade_id" in row for row in perf["realized_by_trade"])
    _no_old_keys(perf)


def test_summary_only_has_an_empty_trade_breakdown() -> None:
    out = executions_reader._performance_response_summary_only(
        fill_count=0, total_realized_pnl=0.0, total_commission=0.0, net_pnl=0.0, win_count=0, loss_count=0
    )
    assert out["realized_by_trade"] == []
    assert "realized_by_strategy_instance" not in out


# --- writers ----------------------------------------------------------------------------


def test_patch_execution_takes_trade_id(two_dbs) -> None:  # noqa: F811
    env, golden = _env(), _golden()
    two_dbs(env, golden)
    out = accounts.patch_execution(CFG, 77, {"strategy_opportunity_id": 5, "trade_id": 41})
    assert out["trade_id"] == 41 and out["fill_splits"] == []
    _no_old_keys(out)
    _, params = env.statement("INSERT INTO trade_execution")
    assert params == (ACCOUNT, "0000e1.01", 41)


@pytest.mark.parametrize("fields", [{"strategy_instance_id": 41}, {"instance_allocations": []}])
def test_patch_execution_refuses_the_old_names(two_dbs, fields: Dict[str, Any]) -> None:  # noqa: F811
    two_dbs(_env(), _golden())
    with pytest.raises(accounts.WriteInvalid, match=next(iter(fields))):
        accounts.patch_execution(CFG, 77, fields)


def test_patch_execution_refuses_a_bad_split(two_dbs) -> None:  # noqa: F811
    two_dbs(_env(), _golden())
    with pytest.raises(accounts.WriteInvalid, match=r"fill_splits must be a list of \{trade_id, quantity\}"):
        accounts.patch_execution(CFG, 77, {"fill_splits": [7]})


def test_review_rows_carry_the_column_names() -> None:
    row = trade_review._row_out({"trade_id": 41, "tags_added_json": '["a"]', "tags_dropped_json": [], "reviewed_at": None})
    assert row["tags_added_json"] == ["a"] and row["tags_dropped_json"] == [] and row["trade_id"] == 41
    assert "tags_added" not in row and "strategy_instance_id" not in row


def test_review_patch_takes_tags_added_json() -> None:
    returned = {"trade_review_id": 1, "trade_id": 41, "tags_added_json": ["b"], "tags_dropped_json": [],
                "reviewed_at": None, "created_at": None, "updated_at": None}
    conn = FakeConn([("SELECT 1 FROM trade WHERE trade_id", Reply(one=(1,))), ("INSERT INTO trade_review", Reply(one=returned))])
    out = trade_review.patch_review(conn, 41, {"tags_added_json": ["b"]})
    sql, params = conn.statement("INSERT INTO trade_review")
    assert "tags_added_json" in sql and params["tags_added_json"] == '["b"]'
    assert out["tags_added_json"] == ["b"] and out["trade_id"] == 41


def test_review_patch_refuses_tags_added() -> None:
    with pytest.raises(trade_review.WriteInvalid, match="tags_added"):
        trade_review.patch_review(FakeConn([]), 41, {"tags_added": ["b"]})


def test_link_fill_body_takes_trade_id_only() -> None:
    assert PlanLinkFillBody(trade_id=41).trade_id == 41
    assert "strategy_instance_id" not in PlanLinkFillBody.model_fields
    with pytest.raises(ValidationError, match="trade_id is required"):
        PlanLinkFillBody(strategy_instance_id=41)


# --- names the R3 release kept one version ------------------------------------------------


def test_the_r3_aliases_are_gone() -> None:
    for name in ("INSTANCE_EXECUTION", "INSTANCE_ALLOCATION", "COMPAT_INSTANCE_ALLOCATIONS", "LEGACY_INSTANCE_ALLOCATION"):
        assert not hasattr(brokerage_tables, name), name
    assert not hasattr(trade_ddl, "STRATEGY_INSTANCE_EXECUTION_DDL")
    assert "instance_allocations" not in brokerage_tables.BROKERAGE_ENV_VIEWS


def test_env_views_have_no_compat_column_and_drop_the_old_view() -> None:
    class Rec:
        def __init__(self) -> None:
            self.sql: list = []

        def execute(self, sql: str, params: Any = None) -> None:
            self.sql.append(sql)

    rec = Rec()
    brokerage_views._create_brokerage_views(rec, "brokerage", env=True)
    text = "\n".join(rec.sql)
    assert "AS strategy_instance_id" not in text
    assert "CREATE OR REPLACE VIEW brokerage.instance_allocations" not in text
    assert rec.sql[0] == "DROP VIEW IF EXISTS brokerage.instance_allocations"
    gs = Rec()
    brokerage_views._create_brokerage_views(gs, "raw_broker")
    assert not any("instance_allocations" in s for s in gs.sql)


def test_ensure_trade_tables_no_longer_makes_the_frozen_table() -> None:
    class Rec:
        def __init__(self) -> None:
            self.sql: list = []

        def execute(self, sql: str, params: Any = None) -> None:
            self.sql.append(sql)

    rec = Rec()
    trade_ddl.ensure_trade_tables(rec)
    assert not any("account_execution_instance_allocation" in s for s in rec.sql)


def test_the_facade_speaks_of_trades_and_keeps_the_old_names_one_version() -> None:
    reader = common_reader.StatusReader
    pairs = {
        "list_strategy_instances": "list_trades",
        "get_strategy_instance_by_id": "get_trade_by_id",
        "create_strategy_instance": "create_trade",
        "get_strategy_win_rate": "get_trade_win_rate",
        "get_performance_instance_summary": "get_performance_trade_summary",
        "get_position_instance_attribution": "get_position_trade_attribution",
    }
    for old, new in pairs.items():
        assert getattr(reader, old) is getattr(reader, new), old
