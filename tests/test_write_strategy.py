"""TD-15 strategy writers: patch_* return the row, *_strict deletes say what happened.

Against scripted fake connections (``write_fakes``); the readers that build the
returned row are replaced, so these tests pin the write rules, not the read SQL.
Ids, names and accounts below are made up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict

import psycopg2
import pytest

from bifrost_core.monitor.reader import gate_safety as gate_safety_reader
from bifrost_core.monitor.reader import gate_safety_write
from bifrost_core.monitor.reader import saved_search
from bifrost_core.monitor.reader import strategy as strategy_reader
from bifrost_core.monitor.reader import strategy_allocation_write as allocation_write
from bifrost_core.monitor.reader import strategy_instance
from bifrost_core.monitor.reader import strategy_opportunity_write as opportunity_write
from bifrost_core.monitor.reader import strategy_plan
from bifrost_core.monitor.reader import strategy_rules_delete as rules
from bifrost_core.monitor.reader import strategy_structure_write as structure_write
from bifrost_core.monitor.reader import template_config
from bifrost_core.monitor.reader import template_config_write as template_write
from bifrost_core.monitor.reader import trade_review
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import (
    WriteConflict,
    WriteFailed,
    WriteInvalid,
    WriteNotFound,
)
from bifrost_core.monitor.reader.saved_search import SavedSearchError
from bifrost_core.monitor.reader.strategy_plan import PlanRuleError
from bifrost_core.monitor.reader.strategy_rules_delete import RuleInUseError
from write_fakes import FakeConn, Reply

CFG = {"sink": "postgres"}
DB_DOWN = psycopg2.OperationalError("server closed the connection unexpectedly")


def test_the_older_rule_errors_are_conflicts_and_still_value_errors() -> None:
    for cls in (RuleInUseError, PlanRuleError):
        err = cls("It is in use.")
        assert isinstance(err, WriteConflict) and isinstance(err, ValueError)
        assert err.reason == "It is in use."
    bad = SavedSearchError("label is required.")
    assert isinstance(bad, WriteInvalid) and isinstance(bad, ValueError)
    assert bad.reason == "label is required."


# --- the UPDATE-then-read-back writers, one table ------------------------------------


class Case:
    def __init__(
        self,
        name: str,
        fn: Callable[..., Dict[str, Any]],
        reader_owner: Any,
        reader: str,
        update: str,
        ok: Dict[str, Any],
        nullable: str,
        not_null: str,
    ) -> None:
        self.name = name
        self.fn = fn
        self.reader_owner = reader_owner
        self.reader = reader
        self.update = update
        self.ok = ok
        self.nullable = nullable
        self.not_null = not_null

    def __repr__(self) -> str:
        return self.name


CASES = [
    Case("instance", strategy_instance.patch_instance, strategy_instance, "get_instance_by_id",
         "UPDATE strategy_instance", {"label": "Roll A"}, "notes", "opened_at"),
    Case("allocation", allocation_write.patch_allocation, strategy_reader, "get_allocation_by_id",
         "UPDATE strategy_allocation", {"name": "Core book"}, "gate_safety_strategy_id", "name"),
    Case("opportunity", opportunity_write.patch_opportunity, strategy_reader, "get_opportunity_by_id",
         "UPDATE strategy_opportunity", {"name": "Wheel"}, "scope_type", "strategy_structure_id"),
    Case("template", template_write.patch_template, template_config, "get_template_detail",
         "UPDATE strategy_template", {"display_name": "Iron condor"}, "explanation", "sort_order"),
    Case("gate set", gate_safety_write.patch_gate_safety, gate_safety_reader, "get_gate_safety_full_by_id",
         "UPDATE gate_safety_strategy", {"name": "Calm tape"}, "dim_risk", "version"),
    Case("structure", structure_write.patch_structure, strategy_reader, "get_structure_by_id",
         "UPDATE strategy_structure", {"name": "CC 30 delta"}, "notes", "name"),
]


@pytest.fixture
def read_back(monkeypatch: pytest.MonkeyPatch):
    """Replace a case's reader with one that answers a recognisable row."""

    def install(case: Case, row: Any = "default") -> None:
        answer = {"id": 41, "read_back": True} if row == "default" else row
        monkeypatch.setattr(case.reader_owner, case.reader, lambda conn, rid: answer)

    return install


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_returns_the_row_the_reader_reads(case: Case, read_back) -> None:
    read_back(case)
    conn = FakeConn()
    row = case.fn(conn, 41, dict(case.ok))
    assert row == {"id": 41, "read_back": True}
    sql, params = conn.statement(case.update)
    field = next(iter(case.ok))
    assert f"{field} = %s" in sql and "updated_at = now()" in sql
    assert params[-1] == 41
    assert conn.commits == 1 and conn.rollbacks == 0


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_refuses_an_empty_body(case: Case) -> None:
    conn = FakeConn()
    with pytest.raises(WriteInvalid, match="Nothing to change"):
        case.fn(conn, 41, {})
    assert conn.executed == []


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_refuses_an_unknown_field(case: Case) -> None:
    conn = FakeConn()
    with pytest.raises(WriteInvalid, match="Unknown .* field: colour"):
        case.fn(conn, 41, {**case.ok, "colour": "teal"})
    assert conn.executed == []


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_explicit_null_clears_a_nullable_column(case: Case, read_back) -> None:
    read_back(case)
    conn = FakeConn()
    case.fn(conn, 41, {case.nullable: None})
    sql, params = conn.statement(case.update)
    assert f"{case.nullable} = %s" in sql
    assert params[0] is None


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_refuses_null_on_a_not_null_column(case: Case) -> None:
    with pytest.raises(WriteInvalid, match=f"{case.not_null} is required"):
        case.fn(FakeConn(), 41, {case.not_null: None})


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_refuses_blank_text_rather_than_storing_null(case: Case) -> None:
    field = next(iter(case.ok))
    with pytest.raises(WriteInvalid):
        case.fn(FakeConn(), 41, {field: "   "})


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_on_a_missing_row_is_not_found(case: Case, read_back) -> None:
    read_back(case)
    conn = FakeConn([(case.update, Reply(rowcount=0))])
    with pytest.raises(WriteNotFound, match="No "):
        case.fn(conn, 404, dict(case.ok))
    assert conn.commits == 0 and conn.rollbacks == 1


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_db_error_is_write_failed(case: Case, read_back) -> None:
    read_back(case)
    conn = FakeConn([(case.update, Reply(raises=DB_DOWN))])
    with pytest.raises(WriteFailed):
        case.fn(conn, 41, dict(case.ok))
    assert conn.commits == 0 and conn.rollbacks == 1


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_without_postgres_is_write_failed(case: Case) -> None:
    with pytest.raises(WriteFailed, match="not configured"):
        case.fn(None, 41, dict(case.ok))


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_patch_that_cannot_read_back_rolls_back(case: Case, read_back) -> None:
    read_back(case, row=None)
    conn = FakeConn()
    with pytest.raises(WriteFailed, match="could not be read back"):
        case.fn(conn, 41, dict(case.ok))
    assert conn.commits == 0 and conn.rollbacks == 1


def test_patch_opens_and_closes_its_own_connection_from_a_config(monkeypatch: pytest.MonkeyPatch, read_back) -> None:
    case = CASES[0]
    read_back(case)
    opened = FakeConn()
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: opened)
    assert case.fn(CFG, 41, {"label": "Roll A"})["read_back"]
    assert opened.commits == 1 and opened.closed


# --- per-resource rules ------------------------------------------------------------


def test_instance_timestamps_take_unix_seconds_or_iso(read_back) -> None:
    read_back(CASES[0])
    conn = FakeConn()
    strategy_instance.patch_instance(conn, 41, {"opened_at": 1767225600, "created_at": "2026-01-01T00:00:00Z"})
    _, params = conn.statement("UPDATE strategy_instance")
    assert params[0] == params[1] == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_allocation_limits_patch_key_by_key_and_membership_is_replaced(read_back) -> None:
    read_back(CASES[1])
    conn = FakeConn()
    allocation_write.patch_allocation(conn, 7, {"allocation_limits": {"max_bp_pct": 25}, "strategy_opportunity_ids": [3, 9]})
    sql, params = conn.statement("UPDATE strategy_allocation")
    assert "max_bp_pct = %s" in sql and "max_positions" not in sql
    assert conn.ran("DELETE FROM strategy_allocation_opportunity")
    inserts = [p for s, p in conn.executed if s.startswith("INSERT INTO strategy_allocation_opportunity")]
    assert inserts == [(7, 3, 0), (7, 9, 1)]


def test_allocation_null_membership_is_refused_not_emptied() -> None:
    with pytest.raises(WriteInvalid, match=r"send \[\]"):
        allocation_write.patch_allocation(FakeConn(), 7, {"strategy_opportunity_ids": None})
    with pytest.raises(WriteInvalid, match="twice"):
        allocation_write.patch_allocation(FakeConn(), 7, {"strategy_opportunity_ids": [3, 3]})
    with pytest.raises(WriteInvalid, match="send it once"):
        allocation_write.patch_allocation(FakeConn(), 7, {"max_positions": 2, "allocation_limits": {"max_positions": 3}})


def test_allocation_unknown_opportunity_is_invalid(read_back) -> None:
    read_back(CASES[1])
    conn = FakeConn([("INSERT INTO strategy_allocation_opportunity", Reply(raises=psycopg2.errors.ForeignKeyViolation("fk")))])
    with pytest.raises(WriteInvalid, match="referenced row does not exist"):
        allocation_write.patch_allocation(conn, 7, {"strategy_opportunity_ids": [999]})


def test_opportunity_patch_keeps_what_it_was_not_sent(read_back) -> None:
    """PUT NULLs the gate and scope and reactivates the rule; PATCH touches only what was sent."""
    read_back(CASES[2])
    conn = FakeConn()
    opportunity_write.patch_opportunity(conn, 5, {"is_active": False})
    sql, params = conn.statement("UPDATE strategy_opportunity")
    assert sql.startswith("UPDATE strategy_opportunity SET is_active = %s, updated_at = now()")
    assert params == [False, 5]


def test_opportunity_lists_are_validated() -> None:
    with pytest.raises(WriteInvalid, match="symbols\\[1\\]"):
        opportunity_write.patch_opportunity(FakeConn(), 5, {"symbols": ["XYZ", " "]})
    with pytest.raises(WriteInvalid, match="condition_type"):
        opportunity_write.patch_opportunity(FakeConn(), 5, {"entry_conditions": [{"value_numeric": 3}]})
    with pytest.raises(WriteInvalid, match=r"send \[\]"):
        opportunity_write.patch_opportunity(FakeConn(), 5, {"entry_conditions": None})


def test_template_code_must_already_be_snake_case_and_unique(read_back) -> None:
    with pytest.raises(WriteInvalid, match="snake_case"):
        template_write.patch_template(FakeConn(), 2, {"template_code": "Iron Condor"})
    with pytest.raises(WriteInvalid, match="dim catalog"):
        template_write.patch_template(FakeConn(), 2, {"dim_risk": "not_a_code"})
    read_back(CASES[3])
    conn = FakeConn([("UPDATE strategy_template", Reply(raises=psycopg2.errors.UniqueViolation("dup")))])
    with pytest.raises(WriteConflict, match="template_code iron_condor is already used"):
        template_write.patch_template(conn, 2, {"template_code": "iron_condor"})


def test_gate_patch_merges_into_the_stored_params(read_back) -> None:
    read_back(CASES[4])
    stored = {"strategy": {"structure": {"min_dte": 20, "max_dte": 45}, "earnings": {"dates": ["2026-11-03"]}}}
    conn = FakeConn([("SELECT params_json FROM gate_safety_strategy", Reply(one={"params_json": stored}))])
    gate_safety_write.patch_gate_safety(conn, 3, {"gates": {"strategy": {"structure": {"max_dte": 60}}}})
    sql, params = conn.statement("UPDATE gate_safety_strategy")
    assert "params_json = %s::jsonb" in sql
    import json

    written = json.loads(params[0])
    assert written["strategy"]["structure"]["min_dte"] == 20
    assert written["strategy"]["structure"]["max_dte"] == 60
    assert written["strategy"]["earnings"]["dates"] == ["2026-11-03"]


def test_gate_patch_replaces_earnings_dates_and_refuses_unknown_or_nested_dates(read_back) -> None:
    read_back(CASES[4])
    conn = FakeConn([("SELECT params_json FROM gate_safety_strategy", Reply(one={"params_json": {}}))])
    gate_safety_write.patch_gate_safety(conn, 3, {"earnings_dates": ["2026-12-01"]})
    import json

    _, params = conn.statement("UPDATE gate_safety_strategy")
    assert json.loads(params[0])["strategy"]["earnings"]["dates"] == ["2026-12-01"]
    with pytest.raises(WriteInvalid, match="Unknown gates field: strategy.structure.max_days"):
        gate_safety_write.patch_gate_safety(FakeConn(), 3, {"gates": {"strategy": {"structure": {"max_days": 9}}}})
    with pytest.raises(WriteInvalid, match="top-level earnings_dates"):
        gate_safety_write.patch_gate_safety(
            FakeConn(), 3, {"gates": {"strategy": {"earnings": {"dates": ["2026-12-01"]}}}}
        )
    with pytest.raises(WriteInvalid, match="YYYY-MM-DD"):
        gate_safety_write.patch_gate_safety(FakeConn(), 3, {"earnings_dates": ["Dec 1"]})


def test_gate_patch_on_a_missing_set_is_not_found_before_the_merge() -> None:
    conn = FakeConn([("SELECT params_json FROM gate_safety_strategy", Reply(one=None))])
    with pytest.raises(WriteNotFound, match="No gate set 3"):
        gate_safety_write.patch_gate_safety(conn, 3, {"earnings_dates": []})


def test_structure_meta_replaces_meta_json(read_back) -> None:
    read_back(CASES[5])
    conn = FakeConn()
    structure_write.patch_structure(conn, 8, {"meta": [{"meta_key": "delta", "meta_value_text": "0.30"}]})
    sql, params = conn.statement("UPDATE strategy_structure")
    assert "meta_json = %s::jsonb" in sql and params[0] == '{"delta": "0.30"}'
    with pytest.raises(WriteInvalid, match="Unknown structure field: legs"):
        structure_write.patch_structure(FakeConn(), 8, {"legs": []})


# --- plans ---------------------------------------------------------------------------


def _plan_conn(status: str, **stored: Any) -> FakeConn:
    current = {"status": status, "target_kind": None, "target_value": None, "stop_kind": None, "stop_value": None}
    current.update(stored)
    return FakeConn([("FROM strategy_plan WHERE strategy_plan_id = %s FOR UPDATE", Reply(one=current))])


@pytest.fixture
def plan_read_back(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(strategy_plan, "_get_plan_on", lambda conn, pid: {"strategy_plan_id": pid, "status": "x"})


def test_plan_draft_takes_any_field_and_returns_the_plan(plan_read_back) -> None:
    conn = _plan_conn("draft")
    row = strategy_plan.patch_plan(conn, 12, {"qty": 2, "rationale": None, "symbol": "xyz"})
    assert row == {"strategy_plan_id": 12, "status": "x"}
    sql, params = conn.statement("UPDATE strategy_plan SET")
    assert "qty = %s" in sql and "rationale = %s" in sql and "symbol = %s" in sql
    assert params[:3] == ["XYZ", 2, None]


def test_plan_intended_may_change_only_its_expiry(plan_read_back) -> None:
    """The plan card's "Extend 7 days" / "Re-issue intent" send {expires_at} alone."""
    conn = _plan_conn("intended")
    strategy_plan.patch_plan(conn, 12, {"expires_at": "2026-10-09T20:00:00Z"})
    sql, params = conn.statement("UPDATE strategy_plan SET")
    assert sql.startswith("UPDATE strategy_plan SET expires_at = %s, updated_at = now()")
    assert params[0] == datetime(2026, 10, 9, 20, tzinfo=timezone.utc)

    conn = _plan_conn("intended")
    with pytest.raises(WriteConflict, match="only its expiry .* not qty, rationale"):
        strategy_plan.patch_plan(conn, 12, {"expires_at": None, "qty": 3, "rationale": "Bigger."})
    assert not conn.ran("UPDATE strategy_plan SET")


@pytest.mark.parametrize("status", ["filled", "cancelled"])
def test_plan_past_intended_cannot_be_patched_at_all(status: str) -> None:
    conn = _plan_conn(status)
    with pytest.raises(WriteConflict, match=f"This plan is {status}"):
        strategy_plan.patch_plan(conn, 12, {"expires_at": None})


def test_plan_input_errors_are_invalid_not_conflicts() -> None:
    for fields in (
        {"qty": 0},
        {"qty": None},
        {"source_kind": None},
        {"target_kind": "moon"},
        {"legs": None},
        {"legs": [{"side": "hold"}]},
        {"exit_by": "next week"},
        {"rationale": "  "},
    ):
        with pytest.raises(WriteInvalid) as err:
            strategy_plan.patch_plan(_plan_conn("draft"), 12, fields)
        assert not isinstance(err.value, WriteConflict)


def test_plan_target_pair_is_checked_against_the_stored_half(plan_read_back) -> None:
    conn = _plan_conn("draft", target_kind="credit_pct", target_value=50)
    with pytest.raises(WriteInvalid, match="target needs both"):
        strategy_plan.patch_plan(conn, 12, {"target_value": None})
    conn = _plan_conn("draft", target_kind="credit_pct", target_value=50)
    strategy_plan.patch_plan(conn, 12, {"target_value": 60})


def test_plan_missing_and_db_error() -> None:
    conn = FakeConn([("FOR UPDATE", Reply(one=None))])
    with pytest.raises(WriteNotFound, match="No plan 12"):
        strategy_plan.patch_plan(conn, 12, {"qty": 1})
    conn = FakeConn([("FOR UPDATE", Reply(raises=DB_DOWN))])
    with pytest.raises(WriteFailed):
        strategy_plan.patch_plan(conn, 12, {"qty": 1})
    with pytest.raises(WriteInvalid, match="Unknown plan field: status"):
        strategy_plan.patch_plan(FakeConn(), 12, {"status": "intended"})


def test_plan_put_still_refuses_the_expiry_on_an_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision B: update_plan (PUT) keeps today's behaviour for one release."""
    conn = FakeConn([("SELECT status FROM strategy_plan", Reply(one=("intended",)))])
    monkeypatch.setattr(strategy_plan, "_conn_from_config", lambda cfg: conn)
    with pytest.raises(PlanRuleError, match="only a draft can be edited"):
        strategy_plan.update_plan(CFG, 12, {"expires_at": None})


def test_delete_plan_strict() -> None:
    conn = FakeConn([("SELECT status FROM strategy_plan", Reply(one=("draft",)))])
    assert strategy_plan.delete_plan_strict(conn, 12) == {"deleted": "hard", "strategy_plan_id": 12}
    assert conn.ran("DELETE FROM strategy_plan") and conn.commits == 1
    conn = FakeConn([("SELECT status FROM strategy_plan", Reply(one=("intended",)))])
    with pytest.raises(WriteConflict, match="only a draft can be deleted"):
        strategy_plan.delete_plan_strict(conn, 12)
    assert not conn.ran("DELETE FROM strategy_plan")
    with pytest.raises(WriteNotFound):
        strategy_plan.delete_plan_strict(FakeConn([("SELECT status", Reply(one=None))]), 12)
    with pytest.raises(WriteFailed):
        strategy_plan.delete_plan_strict(FakeConn([("SELECT status", Reply(raises=DB_DOWN))]), 12)


# --- reviews -------------------------------------------------------------------------

_REVIEW_ROW = {
    "trade_review_id": 1,
    "strategy_instance_id": 41,
    "tags_added": '["early exit"]',
    "tags_dropped": "[]",
    "note": None,
    "reviewed_at": None,
    "created_at": None,
    "updated_at": None,
}


def test_review_patch_upserts_only_the_sent_fields_and_returns_the_row() -> None:
    conn = FakeConn([("FROM strategy_instance", Reply(one=(1,))), ("INSERT INTO trade_review", Reply(one=_REVIEW_ROW))])
    row = trade_review.patch_review(conn, 41, {"note": None, "reviewed": True})
    assert row["tags_added"] == ["early exit"] and row["reviewed"] is False
    sql, params = conn.statement("INSERT INTO trade_review")
    assert "note = EXCLUDED.note" in sql  # null clears (save_review keeps it)
    assert "reviewed_at = CASE" in sql
    assert "tags_added" not in sql.split("DO UPDATE SET")[1].split("RETURNING")[0]
    assert params["note"] is None and params["reviewed"] is True


def test_review_patch_rules() -> None:
    with pytest.raises(WriteInvalid, match="Nothing to change"):
        trade_review.patch_review(FakeConn(), 41, {})
    with pytest.raises(WriteInvalid, match=r"send \[\]"):
        trade_review.patch_review(FakeConn(), 41, {"tags_added": None})
    with pytest.raises(WriteInvalid, match="true or false"):
        trade_review.patch_review(FakeConn(), 41, {"reviewed": None})
    with pytest.raises(WriteInvalid, match="Unknown review field"):
        trade_review.patch_review(FakeConn(), 41, {"score": 3})
    conn = FakeConn([("FROM strategy_instance", Reply(one=None))])
    with pytest.raises(WriteNotFound, match="No trade 41"):
        trade_review.patch_review(conn, 41, {"reviewed": True})
    assert not conn.ran("INSERT INTO trade_review")
    conn = FakeConn([("FROM strategy_instance", Reply(one=(1,))), ("INSERT INTO trade_review", Reply(raises=DB_DOWN))])
    with pytest.raises(WriteFailed):
        trade_review.patch_review(conn, 41, {"reviewed": True})


# --- strict deletes ------------------------------------------------------------------


def test_delete_template_strict_names_the_structures_that_use_it() -> None:
    conn = FakeConn(
        [
            ("FROM strategy_template WHERE strategy_template_id = %s FOR UPDATE", Reply(one=(1,))),
            ("FROM strategy_structure WHERE strategy_template_id", Reply(all=[("CC 30 delta", True), ("Old CC", False)])),
        ]
    )
    with pytest.raises(WriteConflict) as err:
        template_write.delete_template_strict(conn, 2)
    assert "2 structures use this template: CC 30 delta and Old CC (deactivated)" in err.value.reason
    assert not conn.ran("DELETE FROM strategy_template")


def test_delete_template_strict_outcomes() -> None:
    conn = FakeConn([("FOR UPDATE", Reply(one=(1,)))])
    assert template_write.delete_template_strict(conn, 2) == {"deleted": "hard", "strategy_template_id": 2}
    assert conn.ran("DELETE FROM strategy_template") and conn.commits == 1
    with pytest.raises(WriteNotFound, match="No template 2"):
        template_write.delete_template_strict(FakeConn([("FOR UPDATE", Reply(one=None))]), 2)
    with pytest.raises(WriteFailed):
        template_write.delete_template_strict(FakeConn([("FOR UPDATE", Reply(raises=DB_DOWN))]), 2)


def test_delete_structure_strict_is_soft_and_says_so() -> None:
    conn = FakeConn(
        [
            ("SELECT is_active FROM strategy_structure", Reply(one=(True,))),
            ("UPDATE settings SET active_strategy_structure_id = NULL", Reply(rowcount=1)),
        ]
    )
    out = structure_write.delete_structure_strict(conn, 8)
    assert out == {"deleted": "soft", "strategy_structure_id": 8, "was_active": True, "cleared_daemon_setting": True}
    assert conn.ran("SET is_active = false") and not conn.ran("DELETE")
    with pytest.raises(WriteNotFound, match="No structure 8"):
        structure_write.delete_structure_strict(FakeConn([("SELECT is_active", Reply(one=None))]), 8)


@pytest.mark.parametrize(
    ("fn", "key", "in_use", "reason"),
    [
        (rules.delete_opportunity_strict, "strategy_opportunity_id",
         ("FROM strategy_instance WHERE strategy_opportunity_id", Reply(one=(2,))), "It has 2 trades"),
        (rules.delete_allocation_strict, "strategy_allocation_id",
         ("SELECT active_strategy_allocation_id FROM settings", Reply(one=(6,))), "The daemon runs this allocation"),
        (rules.delete_gate_safety_strict, "gate_safety_strategy_id",
         ("FROM strategy_opportunity WHERE default_gate_safety_strategy_id", Reply(one=(1,))), "1 opportunity uses it"),
    ],
)
def test_rule_strict_deletes(fn, key: str, in_use, reason: str) -> None:
    conn = FakeConn([("FOR UPDATE", Reply(one=(1,))), in_use, ("SELECT count(*)", Reply(one=(0,)))])
    with pytest.raises(RuleInUseError, match=reason) as err:
        fn(conn, 6)
    assert isinstance(err.value, WriteConflict)
    assert not any(sql.startswith("DELETE") for sql, _ in conn.executed)

    conn = FakeConn([("FOR UPDATE", Reply(one=(1,))), ("SELECT count(*)", Reply(one=(0,)))])
    assert fn(conn, 6) == {"deleted": "hard", key: 6}
    with pytest.raises(WriteNotFound):
        fn(FakeConn([("FOR UPDATE", Reply(one=None))]), 6)
    with pytest.raises(WriteFailed, match="not configured"):
        fn(None, 6)
    with pytest.raises(WriteFailed):
        fn(FakeConn([("FOR UPDATE", Reply(raises=DB_DOWN))]), 6)


def test_the_bool_rule_delete_still_answers_false_for_a_missing_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules, "_conn_from_config", lambda cfg: FakeConn([("FOR UPDATE", Reply(one=None))]))
    assert rules.delete_opportunity(CFG, 6) is False


def test_delete_saved_search_strict() -> None:
    conn = FakeConn()
    assert saved_search.delete_saved_search_strict(conn, 4) == {"deleted": "hard", "preference_saved_search_id": 4}
    with pytest.raises(WriteNotFound, match="No saved search 4"):
        saved_search.delete_saved_search_strict(FakeConn([("DELETE", Reply(rowcount=0))]), 4)
    with pytest.raises(WriteFailed):
        saved_search.delete_saved_search_strict(FakeConn([("DELETE", Reply(raises=DB_DOWN))]), 4)


# --- strategy instance delete: split allocations and Golden Source attribution ------------


@pytest.fixture
def two_dbs(monkeypatch: pytest.MonkeyPatch):
    """Route ws.connect: the per-env fake, or the Golden Source fake (or a refusal)."""

    def install(env: FakeConn, golden_conn: Any) -> None:
        def route(params, golden=False):
            if not golden:
                return env
            if isinstance(golden_conn, BaseException):
                raise golden_conn
            return golden_conn

        monkeypatch.setattr(ws, "connect", route)

    return install


def _env(*rules_: Any) -> FakeConn:
    return FakeConn([("FROM strategy_instance WHERE strategy_instance_id = %s FOR UPDATE", Reply(one=(1,))), *rules_])


_COUNTS = "FROM strategy_instance_execution WHERE strategy_instance_id = %s"


def test_instance_delete_blocked_by_directly_attributed_executions(two_dbs) -> None:
    env = _env((_COUNTS, Reply(one=(3, 0))))
    golden = FakeConn()
    two_dbs(env, golden)
    with pytest.raises(WriteConflict, match="^3 fills are attributed to this trade.$"):
        strategy_instance.delete_instance_strict(CFG, 41)
    assert not env.ran("DELETE FROM strategy_instance")
    assert env.rollbacks == 1 and env.commits == 0
    # TD-09: this env's table; Golden Source is not read.
    assert env.statement(_COUNTS)[1] == (41,)
    assert golden.executed == []


def test_instance_delete_blocked_by_split_allocations_with_its_own_reason(two_dbs) -> None:
    env = _env((_COUNTS, Reply(one=(0, 2))))
    two_dbs(env, FakeConn())
    with pytest.raises(WriteConflict, match="2 fills are split to this trade"):
        strategy_instance.delete_instance_strict(CFG, 41)
    assert not env.ran("DELETE FROM strategy_instance")


def test_instance_delete_does_not_delete_blind_when_the_count_fails(two_dbs) -> None:
    env = _env((_COUNTS, Reply(raises=DB_DOWN)))
    two_dbs(env, FakeConn())
    with pytest.raises(WriteFailed):
        strategy_instance.delete_instance_strict(CFG, 41)
    assert not env.ran("DELETE FROM strategy_instance")


def test_count_attributed_executions_reads_this_env(two_dbs) -> None:
    env = FakeConn([(_COUNTS, Reply(one=(4, 1)))])
    two_dbs(env, FakeConn())
    assert strategy_instance.count_attributed_executions(CFG, 41) == 4


def test_instance_delete_succeeds_when_nothing_is_attributed(two_dbs) -> None:
    env = _env((_COUNTS, Reply(one=(0, 0))))
    two_dbs(env, FakeConn())
    assert strategy_instance.delete_instance_strict(CFG, 41) == {"deleted": "hard", "strategy_instance_id": 41, "trade_id": 41}
    assert env.ran("DELETE FROM strategy_instance") and env.commits == 1


def test_instance_delete_missing_and_without_config(two_dbs) -> None:
    env = FakeConn([("FOR UPDATE", Reply(one=None))])
    two_dbs(env, FakeConn())
    with pytest.raises(WriteNotFound, match="No trade 41"):
        strategy_instance.delete_instance_strict(CFG, 41)
    with pytest.raises(WriteFailed, match="status config is needed"):
        strategy_instance.delete_instance_strict(FakeConn(), 41)


def test_instance_delete_fk_race_is_a_conflict(two_dbs) -> None:
    env = _env(
        (_COUNTS, Reply(one=(0, 0))),
        ("DELETE FROM strategy_instance", Reply(raises=psycopg2.errors.ForeignKeyViolation("fk"))),
    )
    two_dbs(env, FakeConn())
    with pytest.raises(WriteConflict, match="still reference it"):
        strategy_instance.delete_instance_strict(CFG, 41)
