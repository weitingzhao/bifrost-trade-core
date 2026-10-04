"""TD-15 writers against real Postgres: the SQL the fake-connection tests cannot check.

Marked `db`: `make test` skips it, `make test-all` with PGHOST runs it. Everything
happens inside the fixture's transaction -- the writers' commits and rollbacks are
turned into savepoints -- and is rolled back at teardown. Golden Source's
`raw_broker` tables are created in the same database for the attribution check.
Names, accounts and ids are made up.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from bifrost_core.monitor.reader import strategy_instance
from bifrost_core.monitor.reader import strategy_plan
from bifrost_core.monitor.reader import strategy_rules_delete as rules
from bifrost_core.monitor.reader import strategy_allocation_write as allocation_write
from bifrost_core.monitor.reader import strategy_opportunity_write as opportunity_write
from bifrost_core.monitor.reader import strategy_structure_write as structure_write
from bifrost_core.monitor.reader import gate_safety_write
from bifrost_core.monitor.reader import template_config_write as template_write
from bifrost_core.monitor.reader import trade_review
from bifrost_core.monitor.reader import watchlist
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteConflict, WriteInvalid, WriteNotFound
from bifrost_core.persistence.postgres.brokerage_ddl import ensure_brokerage_schema
from bifrost_core.portfolio.reader import accounts
from bifrost_core.portfolio.reader import instrument_class
from bifrost_core.portfolio.reader import position_categories

pytestmark = pytest.mark.db

ACCOUNT = "U0000001"
CFG = {"sink": "postgres"}


class _Savepointed:
    """The fixture's connection with commit / rollback mapped onto one savepoint."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._run("SAVEPOINT td15")

    def _run(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def cursor(self, **kw: Any) -> Any:
        return self._conn.cursor(**kw)

    def commit(self) -> None:
        self._run("RELEASE SAVEPOINT td15")
        self._run("SAVEPOINT td15")

    def rollback(self) -> None:
        self._run("ROLLBACK TO SAVEPOINT td15")

    def close(self) -> None:
        return None


@pytest.fixture
def db(pg_conn, monkeypatch: pytest.MonkeyPatch) -> _Savepointed:
    conn = _Savepointed(pg_conn)
    ensure_brokerage_schema(conn, log=lambda m: None)
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: conn)
    return conn


def _one(db: _Savepointed, sql: str, params: Any = None) -> Any:
    """Run one seeding statement and keep it (a writer's rollback goes back to here)."""
    with db.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description is not None else None
    db.commit()
    return row


def _seed_rule_chain(db: _Savepointed) -> dict:
    tpl = _one(db, "INSERT INTO strategy_template (template_code, display_name) VALUES ('td15_tpl', 'TD15') "
                   "RETURNING strategy_template_id")[0]
    struct = _one(db, "INSERT INTO strategy_structure (name, strategy_template_id) VALUES ('TD15 struct', %s) "
                      "RETURNING strategy_structure_id", (tpl,))[0]
    gate = _one(db, "INSERT INTO gate_safety_strategy (name) VALUES ('TD15 gate') RETURNING gate_safety_strategy_id")[0]
    opp = _one(db, "INSERT INTO strategy_opportunity (name, strategy_structure_id, default_gate_safety_strategy_id, scope_type) "
                   "VALUES ('TD15 opp', %s, %s, 'explicit_symbols') RETURNING strategy_opportunity_id", (struct, gate))[0]
    inst = _one(db, "INSERT INTO trade (strategy_opportunity_id, account_id, opened_at, label) "
                    "VALUES (%s, %s, now(), 'L') RETURNING trade_id", (opp, ACCOUNT))[0]
    alloc = _one(db, "INSERT INTO strategy_allocation (name, gate_safety_strategy_id, max_positions, max_bp_pct) "
                     "VALUES ('TD15 alloc', %s, 3, 20) RETURNING strategy_allocation_id", (gate,))[0]
    return {"tpl": tpl, "struct": struct, "gate": gate, "opp": opp, "inst": inst, "alloc": alloc}


def test_watchlist_upsert_keeps_category_and_label_in_postgres(db) -> None:
    cat = _one(db, "INSERT INTO preference_position_categories (name) VALUES ('TD15 list') RETURNING id")[0]
    out = watchlist.upsert_watchlist(db, "TDXW", {"category_id": cat})
    assert out["category"] == "TD15 list" and out["source"] == "manual" and out["symbol"] == "TDXW"
    out = watchlist.upsert_watchlist(db, "TDXW", {"optionable": True})
    assert out["category_id"] == cat and out["optionable"] is True
    out = watchlist.patch_watchlist_item(db, "TDXW|STK|||", {"display_label": "Swing", "category_id": None})
    assert out["display_label"] == "Swing" and out["category_id"] is None
    assert watchlist.delete_watchlist_strict(db, "TDXW")["deleted"] == "hard"
    with pytest.raises(WriteNotFound):
        watchlist.delete_watchlist_strict(db, "TDXW")
    with pytest.raises(WriteInvalid):
        watchlist.upsert_watchlist(db, "TDXZ", {"category_id": 2_000_000_000})


def test_strategy_patches_in_postgres(db) -> None:
    ids = _seed_rule_chain(db)
    row = strategy_instance.patch_instance(db, ids["inst"], {"label": "Roll A"})
    assert row["label"] == "Roll A" and "notes" not in row
    row = strategy_instance.patch_instance(db, ids["inst"], {"label": None})
    assert row["label"] is None

    row = allocation_write.patch_allocation(db, ids["alloc"], {"allocation_limits": {"max_bp_pct": 25}, "strategy_opportunity_ids": [ids["opp"]]})
    assert row["max_positions"] == 3 and row["max_bp_pct"] == 25.0 and row["strategy_opportunity_ids"] == [ids["opp"]]

    row = opportunity_write.patch_opportunity(db, ids["opp"], {"is_active": False, "symbols": ["TDXV"]})
    assert row["is_active"] is False and row["symbols"] == ["TDXV"]
    assert row["default_gate_safety_strategy_id"] == ids["gate"] and row["scope_type"] == "explicit_symbols"

    row = template_write.patch_template(db, ids["tpl"], {"explanation": "Why.", "dim_risk": None})
    assert row["explanation"] == "Why." and row["legs"] == []

    _one(db, "UPDATE gate_safety_strategy SET params_json = %s::jsonb WHERE gate_safety_strategy_id = %s",
         (json.dumps({"strategy": {"structure": {"min_dte": 21}, "earnings": {"dates": ["2026-11-03"]}}}), ids["gate"]))
    row = gate_safety_write.patch_gate_safety(db, ids["gate"], {"gates": {"strategy": {"structure": {"max_dte": 50}}}})
    assert row["gates"]["strategy"]["structure"]["min_dte"] == 21
    assert row["gates"]["strategy"]["structure"]["max_dte"] == 50
    assert row["earnings_dates"] == ["2026-11-03"]

    row = structure_write.patch_structure(db, ids["struct"], {"notes": "Keep", "meta": [{"meta_key": "delta", "meta_value_text": "0.3"}]})
    assert row["notes"] == "Keep" and row["metadata"] == {"delta": "0.3"}

    with pytest.raises(WriteInvalid):
        opportunity_write.patch_opportunity(db, ids["opp"], {"strategy_structure_id": 2_000_000_000})
    with pytest.raises(WriteNotFound):
        strategy_instance.patch_instance(db, 2_000_000_000, {"label": "x"})


def test_plan_patch_and_strict_delete_in_postgres(db) -> None:
    pid = _one(db, "INSERT INTO strategy_plan (account_id, symbol, structure_label, qty, legs_json, target_kind, target_value) "
                   "VALUES (%s, 'TDXV', 'Short put', 1, '[]'::jsonb, 'credit_pct', 50) RETURNING strategy_plan_id", (ACCOUNT,))[0]
    row = strategy_plan.patch_plan(db, pid, {"qty": 2, "rationale": "Room to add.", "exit_by": "2026-11-20"})
    assert row["qty"] == 2 and row["rationale"] == "Room to add."
    _one(db, "UPDATE strategy_plan SET status = 'intended', intended_at = now() WHERE strategy_plan_id = %s", (pid,))
    row = strategy_plan.patch_plan(db, pid, {"expires_at": "2026-10-09T20:00:00Z"})
    assert row["status"] == "intended" and row["expires_at"] is not None
    with pytest.raises(WriteConflict):
        strategy_plan.patch_plan(db, pid, {"qty": 3})
    with pytest.raises(WriteConflict):
        strategy_plan.delete_plan_strict(db, pid)


def test_review_patch_in_postgres(db) -> None:
    ids = _seed_rule_chain(db)
    row = trade_review.patch_review(db, ids["inst"], {"tags_added": ["early exit"]})
    assert row["tags_added"] == ["early exit"] and "note" not in row and row["reviewed"] is False
    row = trade_review.patch_review(db, ids["inst"], {"reviewed": True})
    assert row["tags_added"] == ["early exit"] and row["reviewed"] is True
    with pytest.raises(WriteNotFound):
        trade_review.patch_review(db, 2_000_000_000, {"reviewed": True})


def test_strict_deletes_in_postgres(db) -> None:
    ids = _seed_rule_chain(db)
    with pytest.raises(WriteConflict, match="TD15 struct"):
        template_write.delete_template_strict(db, ids["tpl"])
    with pytest.raises(WriteConflict, match="It has 1 trade"):
        rules.delete_opportunity_strict(db, ids["opp"])
    with pytest.raises(WriteConflict, match="use it"):
        rules.delete_gate_safety_strict(db, ids["gate"])
    assert rules.delete_allocation_strict(db, ids["alloc"]) == {"deleted": "hard", "strategy_allocation_id": ids["alloc"]}
    with pytest.raises(WriteNotFound):
        rules.delete_allocation_strict(db, ids["alloc"])


def test_instance_delete_reads_this_envs_attribution(db) -> None:
    ids = _seed_rule_chain(db)
    # Golden Source's columns no longer count (TD-09): this env's table does.
    _one(db, "INSERT INTO raw_broker.executions_raw_flex (exec_id, account_id, symbol, strategy_instance_id) "
             "VALUES ('td15.e9', %s, 'TDXV', %s)", (ACCOUNT, ids["inst"]))
    assert strategy_instance.count_attributed_executions(CFG, ids["inst"]) == 0
    _one(db, "INSERT INTO trade_execution (account_id, exec_id, trade_id) "
             "VALUES (%s, 'td15.e1', %s), (%s, 'td15.e2', %s)", (ACCOUNT, ids["inst"], ACCOUNT, ids["inst"]))
    assert strategy_instance.count_attributed_executions(CFG, ids["inst"]) == 2
    with pytest.raises(WriteConflict, match="^2 fills are attributed to this trade.$"):
        strategy_instance.delete_instance_strict(CFG, ids["inst"])
    assert _one(db, "SELECT 1 FROM trade WHERE trade_id = %s", (ids["inst"],)) == (1,)
    _one(db, "DELETE FROM trade_execution WHERE exec_id LIKE 'td15.%%'")
    assert strategy_instance.delete_instance_strict(CFG, ids["inst"])["deleted"] == "hard"


def test_execution_patch_and_strict_delete_in_postgres(db) -> None:
    ids = _seed_rule_chain(db)
    raw_id = _one(db, "INSERT INTO raw_broker.executions_raw_flex (exec_id, account_id, symbol, side, quantity, source) "
                      "VALUES ('td15.e3', %s, 'TDXV', 'BUY', 2, 'flex') RETURNING executions_raw_flex_id", (ACCOUNT,))[0]
    out = accounts.patch_execution(CFG, raw_id, {"strategy_opportunity_id": ids["opp"], "strategy_instance_id": ids["inst"]})
    assert out["strategy_instance_id"] == ids["inst"] and out["instance_allocations"] == []
    out = accounts.patch_execution(
        CFG, raw_id, {"instance_allocations": [{"strategy_instance_id": ids["inst"], "allocated_quantity": 2}]}
    )
    assert out["strategy_instance_id"] is None and out["instance_allocations"][0]["allocated_quantity"] == 2.0
    with pytest.raises(WriteConflict, match="split across 1 trade"):
        accounts.patch_execution(CFG, raw_id, {"strategy_instance_id": ids["inst"]})
    _one(db, "INSERT INTO raw_broker.commissions (exec_id, commission) VALUES ('td15.e3', 1)")
    out = accounts.delete_execution_strict(CFG, raw_id)
    assert out == {"deleted": "hard", "account_executions_id": raw_id, "allocations_removed": 1}
    assert _one(db, "SELECT 1 FROM raw_broker.commissions WHERE exec_id = 'td15.e3'") is None


def test_portfolio_preferences_in_postgres(db) -> None:
    cat = _one(db, "INSERT INTO preference_position_categories (name, description) VALUES ('TD15 cat', 'd') RETURNING id")[0]
    row = position_categories.patch_position_category(db, cat, {"description": None, "sort_order": 4})
    assert row["description"] is None and row["sort_order"] == 4
    _one(db, "INSERT INTO preference_position_category_tags (account_id, contract_key, category_id) VALUES (%s, 'TDXV|STK|||', %s)",
         (ACCOUNT, cat))
    assert position_categories.delete_position_category_strict(db, cat)["tags_removed"] == 1

    _one(db, "INSERT INTO preference_instrument_class (contract_key, instrument_class, note) VALUES ('TDXB|STK|||', 'stock', 'n')")
    row = instrument_class.patch_instrument_class(db, "TDXB|STK|||", {"instrument_class": "cash_like", "note": None})
    assert row["instrument_class"] == "cash_like" and row["note"] is None
    assert instrument_class.delete_instrument_class_strict(db, "TDXB|STK|||")["deleted"] == "hard"
