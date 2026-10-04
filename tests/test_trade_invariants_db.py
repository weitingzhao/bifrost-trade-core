"""TD-43 / TD-56 / TD-71 against real Postgres (core 0.41.0): the constraints, the cascade and the
derived instance state.

Marked `db` (``make test-db``). Golden Source's ``raw_broker`` tables live in the same database
and ``brokerage`` holds pass-through views of them standing in for the FDW tables, as in the
TD-09 tests. Everything runs in the fixture's transaction and is rolled back. Accounts,
symbols, contracts and exec ids are made up.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import psycopg2
import pytest

from bifrost_core.monitor.reader import strategy_instance, strategy_plan
from bifrost_core.monitor.reader import strategy_opportunity_write as opportunity_write
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteConflict, WriteInvalid
from bifrost_core.monitor.reader.instance_state import instance_states
from bifrost_core.persistence.postgres.brokerage_ddl import _create_brokerage_views, ensure_brokerage_schema
from bifrost_core.portfolio.reader import position_categories

pytestmark = pytest.mark.db

ACCT = "U0000001"
CFG = {"sink": "postgres"}
RAW = ("executions_raw_flex", "executions_raw_tws", "executions_raw_journal")
OPENED = datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)


class _Savepointed:
    """The fixture's connection with commit / rollback mapped onto one savepoint."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._run("SAVEPOINT k43")

    def _run(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def cursor(self, **kw: Any) -> Any:
        return self._conn.cursor(**kw)

    def commit(self) -> None:
        self._run("RELEASE SAVEPOINT k43")
        self._run("SAVEPOINT k43")

    def rollback(self) -> None:
        self._run("ROLLBACK TO SAVEPOINT k43")

    def close(self) -> None:
        return None


@pytest.fixture
def db(pg_conn, monkeypatch: pytest.MonkeyPatch) -> _Savepointed:
    conn = _Savepointed(pg_conn)
    ensure_brokerage_schema(conn, log=lambda m: None)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS brokerage CASCADE")
        cur.execute("CREATE SCHEMA brokerage")
        for t in RAW:
            cur.execute(f"CREATE VIEW brokerage.{t} AS SELECT * FROM raw_broker.{t}")
        _create_brokerage_views(cur, "brokerage", env=True)
    conn.commit()
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: conn)
    return conn


def _one(db: _Savepointed, sql: str, params: Any = None) -> Any:
    with db.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description is not None else None
    db.commit()
    return row


def _opportunity(db: _Savepointed, name: str = "k43") -> int:
    struct = _one(db, "INSERT INTO strategy_structure (name) VALUES (%s) RETURNING strategy_structure_id", (name,))[0]
    return _one(
        db,
        "INSERT INTO strategy_opportunity (name, strategy_structure_id, scope_type, symbols_json) "
        "VALUES (%s, %s, 'explicit_symbols', '[\"ZZZQ\"]') RETURNING strategy_opportunity_id",
        (name, struct),
    )[0]


def _instance(db: _Savepointed, opp: int) -> int:
    return _one(
        db,
        "INSERT INTO trade (strategy_opportunity_id, account_id, opened_at) VALUES (%s, %s, %s) "
        "RETURNING trade_id",
        (opp, ACCT, OPENED),
    )[0]


def _intended_plan(db: _Savepointed) -> int:
    return _one(
        db,
        "INSERT INTO strategy_plan (account_id, symbol, structure_label, qty, status, exit_by) "
        "VALUES (%s, 'ZZZQ', 'Short put', 1, 'intended', '2026-10-16') RETURNING strategy_plan_id",
        (ACCT,),
    )[0]


def _opt_fill(
    db: _Savepointed,
    exec_id: str,
    inst: int | None,
    *,
    side: str,
    qty: float,
    expiry: str,
    strike: float = 80.0,
    right: str = "P",
    traded: date = date(2026, 9, 1),
    allocated: float | None = None,
) -> None:
    key = f"ZZZQ|OPT|{expiry}|{strike}|{right}"
    _one(
        db,
        "INSERT INTO raw_broker.executions_raw_flex (exec_id, account_id, symbol, sec_type, side, quantity, source, "
        "contract_key, expiry, strike, option_right, trade_date) "
        "VALUES (%s, %s, 'ZZZQ', 'OPT', %s, %s, 'flex_trades', %s, %s, %s, %s, %s)",
        (exec_id, ACCT, side, qty if side == "BUY" else -qty, key, expiry, strike, right, traded),
    )
    if inst is not None:
        _one(
            db,
            "INSERT INTO trade_execution (account_id, exec_id, trade_id, split_quantity) "
            "VALUES (%s, %s, %s, %s)",
            (ACCT, exec_id, inst, allocated),
        )


# --- TD-43: plan / review invariants ------------------------------------------------------


def test_the_table_holds_filled_and_the_instance_together(db) -> None:
    inst = _instance(db, _opportunity(db))
    with pytest.raises(psycopg2.errors.CheckViolation):
        _one(db, "INSERT INTO strategy_plan (account_id, symbol, structure_label, qty, status) "
                 "VALUES (%s, 'ZZZQ', 'Put', 1, 'filled')", (ACCT,))
    db.rollback()
    with pytest.raises(psycopg2.errors.CheckViolation):
        _one(db, "INSERT INTO strategy_plan (account_id, symbol, structure_label, qty, status, trade_id) "
                 "VALUES (%s, 'ZZZQ', 'Put', 1, 'intended', %s)", (ACCT, inst))
    db.rollback()


def test_a_filled_plan_reads_its_instance_open_and_keeps_its_instance(db, monkeypatch) -> None:
    monkeypatch.setattr(strategy_plan, "_conn_from_config", lambda _cfg: db)
    inst = _instance(db, _opportunity(db))
    plan = _intended_plan(db)
    assert strategy_plan.link_fill(CFG, plan, inst) is True
    row = strategy_plan.get_plan(CFG, plan)
    assert row["status"] == "filled" and row["trade_id"] == inst
    assert row["filled_at"] == OPENED
    # Not stored at all since core 0.43.0: a fresh schema has no such column (TD-43).
    assert _one(db, "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = 'strategy_plan' AND column_name = 'filled_at'") == (0,)
    # Moving the instance's open moves the plan's fill time: one stored value, not two.
    moved = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
    strategy_instance.patch_instance(CFG, inst, {"opened_at": moved.isoformat()})
    assert strategy_plan.get_plan(CFG, plan)["filled_at"] == moved
    assert [p["filled_at"] for p in strategy_plan.list_plans(CFG, status="filled", symbol="zzzq", account_id=ACCT)] == [moved]
    # The instance cannot go while the plan points at it: not by SQL, not by core.
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _one(db, "DELETE FROM trade WHERE trade_id = %s", (inst,))
    db.rollback()
    with pytest.raises(WriteConflict, match="1 plan was filled by it"):
        strategy_instance.delete_instance_strict(CFG, inst)
    assert _one(db, "SELECT count(*) FROM trade WHERE trade_id = %s", (inst,)) == (1,)


def test_a_review_is_never_deleted_with_its_instance(db) -> None:
    inst = _instance(db, _opportunity(db))
    _one(db, "INSERT INTO trade_review (trade_id) VALUES (%s)", (inst,))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _one(db, "DELETE FROM trade WHERE trade_id = %s", (inst,))
    db.rollback()
    with pytest.raises(WriteConflict, match="it has a review"):
        strategy_instance.delete_instance_strict(CFG, inst)
    assert _one(db, "SELECT count(*) FROM trade_review WHERE trade_id = %s", (inst,)) == (1,)


def test_an_instance_nothing_points_at_is_still_deleted(db) -> None:
    inst = _instance(db, _opportunity(db))
    # naming R4 (core 0.47.0): the answer names the trade only
    assert strategy_instance.delete_instance_strict(CFG, inst) == {"deleted": "hard", "trade_id": inst}


# --- TD-43: derived state ------------------------------------------------------------------


def test_the_instance_list_carries_the_state_its_fills_say(db) -> None:
    opp = _opportunity(db)
    closed, expired, open_, empty, split = (_instance(db, opp) for _ in range(5))
    # closed: sold to open, bought back.
    _opt_fill(db, "k43.c1", closed, side="SELL", qty=1, expiry="20261016", traded=date(2026, 9, 1))
    _opt_fill(db, "k43.c2", closed, side="BUY", qty=1, expiry="20261016", traded=date(2026, 9, 12))
    # expired: sold, never bought back, expiry passed.
    _opt_fill(db, "k43.e1", expired, side="SELL", qty=2, expiry="20260918", strike=75.0)
    # open: a leg that has not expired yet.
    _opt_fill(db, "k43.o1", open_, side="SELL", qty=1, expiry="20261120", strike=70.0)
    # split: this instance's share of a 3-lot sell is 1, and its own 1-lot buy closes it.
    _opt_fill(db, "k43.s1", split, side="SELL", qty=3, expiry="20261016", strike=85.0, allocated=-1)
    _opt_fill(db, "k43.s2", split, side="BUY", qty=1, expiry="20261016", strike=85.0, traded=date(2026, 9, 20))
    today = date(2026, 10, 3)
    with db.cursor() as cur:
        states = instance_states(cur, [closed, expired, open_, empty, split], today=today)
    assert states == {
        closed: ("closed", date(2026, 9, 12)),
        expired: ("expired", date(2026, 9, 18)),
        open_: ("open", None),
        empty: ("no_fills", None),
        split: ("closed", date(2026, 9, 20)),
    }
    rows = {r["trade_id"]: r for r in strategy_instance.list_instances(db, strategy_opportunity_id=opp)}
    assert rows[closed]["state"] == "closed" and rows[closed]["closed_on"] == "2026-09-12"
    assert rows[empty]["state"] == "no_fills" and rows[empty]["closed_on"] is None
    assert rows[open_]["state"] == "open"


# --- TD-56: categories ---------------------------------------------------------------------


def _category(db: _Savepointed, name: str) -> int:
    return _one(db, "INSERT INTO preference_position_categories (name) VALUES (%s) RETURNING id", (name,))[0]


def _order(db: _Savepointed, name: str) -> list:
    with db.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM preference_market_streams_symbol_order WHERE category_name = %s ORDER BY sort_order",
            (name,),
        )
        return [r[0] for r in cur.fetchall()]


def test_a_rename_carries_the_symbol_order_and_a_delete_removes_it(db) -> None:
    cat = _category(db, "K56 Yield")
    position_categories.set_market_streams_symbol_order(db, "K56 Yield", ["ZZZQ", "ZZZR"])
    position_categories.set_market_streams_symbol_order(db, "K56 Income", ["STALE"])  # no category has this name
    row = position_categories.patch_position_category(db, cat, {"name": "K56 Income"})
    assert row["name"] == "K56 Income"
    assert _order(db, "K56 Income") == ["ZZZQ", "ZZZR"] and _order(db, "K56 Yield") == []
    out = position_categories.delete_position_category_strict(db, cat)
    assert out["symbol_order_removed"] == 2 and _order(db, "K56 Income") == []


def test_a_name_is_unique_and_uncategorized_is_reserved(db) -> None:
    _category(db, "K56 Twin")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _category(db, "K56 Twin")
    db.rollback()
    with pytest.raises(WriteConflict, match="already exists"):
        position_categories.create_position_category(db, "K56 Twin")
    other = _category(db, "K56 Other")
    with pytest.raises(WriteConflict, match="already exists"):
        position_categories.patch_position_category(db, other, {"name": "K56 Twin"})
    with pytest.raises(WriteInvalid, match="reserved"):
        position_categories.patch_position_category(db, other, {"name": "Uncategorized"})
    with pytest.raises(WriteInvalid, match="reserved"):
        position_categories.create_position_category(db, "uncategorized")
    new_id, err = position_categories.create_position_category(db, "K56 Fresh")
    assert err is None and new_id is not None


# --- TD-71: scope_type ---------------------------------------------------------------------


def test_scope_type_is_checked_by_the_table_and_by_core(db) -> None:
    opp = _opportunity(db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _one(db, "UPDATE strategy_opportunity SET scope_type = 'symbols' WHERE strategy_opportunity_id = %s", (opp,))
    db.rollback()
    with pytest.raises(WriteInvalid, match="scope_type must be one of"):
        opportunity_write.patch_opportunity(db, opp, {"scope_type": "symbols"})
    # watchlist_stk needs a symbol, checked on the row as it would be after the change.
    with pytest.raises(WriteInvalid, match="at least one symbol"):
        opportunity_write.patch_opportunity(db, opp, {"scope_type": "watchlist_stk", "symbols": []})
    assert _one(db, "SELECT scope_type FROM strategy_opportunity WHERE strategy_opportunity_id = %s", (opp,)) == (
        "explicit_symbols",
    )
    row = opportunity_write.patch_opportunity(db, opp, {"scope_type": "watchlist_stk"})  # keeps ["ZZZQ"]
    assert row["scope_type"] == "watchlist_stk"
    with pytest.raises(WriteInvalid, match="at least one symbol"):
        opportunity_write.patch_opportunity(db, opp, {"symbols": []})
    row = opportunity_write.patch_opportunity(db, opp, {"scope_type": ""})
    assert row["scope_type"] is None
