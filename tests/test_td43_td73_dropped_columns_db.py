"""TD-43 / TD-73 against real Postgres (core 0.43.0): core works with and without the three columns.

DEV / STG / PROD still have ``strategy_plan.filled_at``, ``strategy_instance.notes`` and
``trade_review.note`` when this release is delivered; the Owner drops them afterwards (infra
db-step ``2026-10-03-td43-td73-drop-columns``). So each case runs twice: on the fresh schema
(no columns) and on the schema as the databases have it today (columns added back here).
db-init must neither fail on the old schema nor add the columns to the new one.

Marked ``db`` (``make test-db``). Everything runs in the fixture's transaction and is rolled
back. Accounts, symbols and dates are made up.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterator

import pytest

from bifrost_core.monitor.reader import strategy_instance, strategy_plan, trade_review
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.persistence.postgres.brokerage_ddl import _create_brokerage_views, ensure_brokerage_schema
from bifrost_core.persistence.postgres.ddl import _ensure_tables

pytestmark = pytest.mark.db

ACCT = "U0000001"
CFG = {"sink": "postgres"}
RAW = ("executions_raw_flex", "executions_raw_tws", "executions_raw_journal")
OPENED = datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)
# strategy_instance is ``trade`` since naming R3 (core 0.45.0).
DROPPED = (("strategy_plan", "filled_at", "timestamptz"), ("trade", "notes", "text"), ("trade_review", "note", "text"))


class _Savepointed:
    """The fixture's connection with commit / rollback mapped onto one savepoint."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._run("SAVEPOINT td73")

    def _run(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def cursor(self, **kw: Any) -> Any:
        return self._conn.cursor(**kw)

    def commit(self) -> None:
        self._run("RELEASE SAVEPOINT td73")
        self._run("SAVEPOINT td73")

    def rollback(self) -> None:
        self._run("ROLLBACK TO SAVEPOINT td73")

    def close(self) -> None:
        return None


def _present(conn: Any) -> Dict[str, bool]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name || '.' || column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND (table_name, column_name) IN "
            "(('strategy_plan', 'filled_at'), ('trade', 'notes'), ('trade_review', 'note'))"
        )
        found = {r[0] for r in cur.fetchall()}
    return {f"{t}.{c}": f"{t}.{c}" in found for t, c, _ in DROPPED}


def _drop_committed(pg_conn: Any) -> None:
    """Back to the fresh schema. ``_ensure_tables`` commits, so an added column outlives the test."""
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        for table, column, _ in DROPPED:
            cur.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
    pg_conn.commit()


@pytest.fixture(params=["dropped", "still_there"])
def db(request, pg_conn, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Savepointed]:
    if request.param == "still_there":
        with pg_conn.cursor() as cur:
            for table, column, kind in DROPPED:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        pg_conn.commit()  # _ensure_tables starts with a rollback
        _ensure_tables(pg_conn)  # an old database's db-init: no error, and the columns stay
        assert all(_present(pg_conn).values())
    else:
        assert not any(_present(pg_conn).values())
    conn = _Savepointed(pg_conn)
    # list_instances counts fills through brokerage.*: pass-through views of Golden Source's
    # raw tables, as in test_trade_invariants_db.
    ensure_brokerage_schema(conn, log=lambda m: None)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS brokerage CASCADE")
        cur.execute("CREATE SCHEMA brokerage")
        for t in RAW:
            cur.execute(f"CREATE VIEW brokerage.{t} AS SELECT * FROM raw_broker.{t}")
        _create_brokerage_views(cur, "brokerage", env=True)
    conn.commit()
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: conn)
    monkeypatch.setattr(strategy_plan, "_conn_from_config", lambda _cfg: conn)
    monkeypatch.setattr(trade_review, "_conn_from_config", lambda _cfg: conn)
    try:
        yield conn
    finally:
        if request.param == "still_there":
            _drop_committed(pg_conn)


def _one(db: _Savepointed, sql: str, params: Any = None) -> Any:
    with db.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description is not None else None
    db.commit()
    return row


def _opportunity(db: _Savepointed) -> int:
    struct = _one(db, "INSERT INTO strategy_structure (name) VALUES ('td73') RETURNING strategy_structure_id")[0]
    return _one(
        db,
        "INSERT INTO strategy_opportunity (name, strategy_structure_id) VALUES ('td73', %s) "
        "RETURNING strategy_opportunity_id",
        (struct,),
    )[0]


def test_db_init_never_adds_the_columns_back(pg_conn) -> None:
    _drop_committed(pg_conn)  # what the Owner's db-step leaves
    _ensure_tables(pg_conn)
    _ensure_tables(pg_conn)
    assert not any(_present(pg_conn).values())


def test_trade_plan_and_review_round_trip(db) -> None:
    opp = _opportunity(db)
    inst = strategy_instance.create_instance(db, opp, ACCT, OPENED, label="ZZQ Oct 40P")
    assert inst is not None
    row = strategy_instance.get_instance_by_id(db, inst)
    assert row["label"] == "ZZQ Oct 40P" and "notes" not in row
    assert [r["strategy_instance_id"] for r in strategy_instance.list_instances(db, account_id=ACCT)] == [inst]
    assert strategy_instance.update_instance(db, inst, label="ZZQ Oct 40P roll") is True
    row = strategy_instance.patch_instance(CFG, inst, {"label": "ZZQ roll"})
    assert row["label"] == "ZZQ roll"

    plan = _one(
        db,
        "INSERT INTO strategy_plan (account_id, symbol, structure_label, qty, status) "
        "VALUES (%s, 'ZZZQ', 'Short put', 1, 'intended') RETURNING strategy_plan_id",
        (ACCT,),
    )[0]
    assert strategy_plan.get_plan(CFG, plan)["filled_at"] is None
    assert strategy_plan.link_fill(CFG, plan, inst) is True
    assert strategy_plan.get_plan(CFG, plan)["filled_at"] == OPENED
    moved = datetime(2026, 9, 2, 15, 0, tzinfo=timezone.utc)
    strategy_instance.patch_instance(CFG, inst, {"opened_at": moved.isoformat()})
    assert [p["filled_at"] for p in strategy_plan.list_plans(CFG, status="filled", account_id=ACCT)] == [moved]

    review = trade_review.patch_review(CFG, inst, {"tags_added": ["early exit"], "reviewed": True})
    assert review["reviewed"] is True and "note" not in review
    assert trade_review.save_review(CFG, inst, {"reviewed": False})["reviewed"] is False
    assert [r["strategy_instance_id"] for r in trade_review.list_reviews(CFG)] == [inst]


def test_old_columns_stay_unwritten_while_they_exist(db, pg_conn) -> None:
    if not all(_present(pg_conn).values()):
        pytest.skip("the dropped schema has nothing to check")
    opp = _opportunity(db)
    inst = strategy_instance.create_instance(db, opp, ACCT, OPENED)
    plan = _one(
        db,
        "INSERT INTO strategy_plan (account_id, symbol, structure_label, qty, status) "
        "VALUES (%s, 'ZZZQ', 'Short put', 1, 'intended') RETURNING strategy_plan_id",
        (ACCT,),
    )[0]
    strategy_plan.link_fill(CFG, plan, inst)
    trade_review.patch_review(CFG, inst, {"reviewed": True})
    assert _one(db, "SELECT count(notes) FROM trade") == (0,)
    assert _one(db, "SELECT count(filled_at) FROM strategy_plan") == (0,)
    assert _one(db, "SELECT count(note) FROM trade_review") == (0,)
