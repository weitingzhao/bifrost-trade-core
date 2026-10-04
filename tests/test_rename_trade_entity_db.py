"""Naming R3 against real Postgres: the reverse and the forward rename round-trip (core 0.45.0).

The fresh schema (core 0.45.0's ``_ensure_tables``) is taken back to the core 0.44.0 names by
``reverse_statements`` and forward again by ``forward_statements``; the catalog ends where it
started (plus the two compatibility views), every row survives, and each script's own report
check (RAISE on a changed count) passes. On the reversed database ``_ensure_tables`` refuses to
run; on the renamed one it changes nothing. Pods on core 0.44.0 work through the compatibility
objects -- including the whole-fill upsert ``ON CONFLICT (account_id, exec_id) WHERE
allocated_quantity IS NULL`` through the view ``strategy_instance_execution`` (pack §5.4's
open question).

The env guards (database name, view owner) and the role switches are left out here: the
throwaway database is not ``bifrost_<env>`` and runs as one superuser. The full scripts, with
them, are rehearsed on a copy of DEV's schema (infra db-steps 2026-10-04-r3-rename-trade-entity).
Marked ``db``; everything is rolled back. Accounts, symbols and exec ids are made up.
"""

from __future__ import annotations

from typing import Any, Dict, List

import psycopg2
import pytest

from bifrost_core.persistence.postgres.brokerage_ddl import _create_brokerage_views, ensure_brokerage_schema
from bifrost_core.persistence.postgres.rename_trade_entity import forward_statements
from bifrost_core.persistence.postgres.rename_trade_entity_reverse import reverse_statements
from bifrost_core.persistence.postgres.trade_ddl import ensure_trade_tables, refuse_unmigrated_trade_entity

pytestmark = pytest.mark.db

ACCT = "U0000001"
RAW = ("executions_raw_flex", "executions_raw_tws", "executions_raw_journal")
TABLES = ("trade", "trade_execution", "strategy_plan", "trade_review", "account_execution_instance_allocation")
OLD_TABLES = ("strategy_instance", "strategy_instance_execution", "strategy_plan", "trade_review",
              "account_execution_instance_allocation")


def _runnable(statements: List[str]) -> List[str]:
    """The script's statements without the env guard and the role switches (see the docstring)."""
    out = []
    for s in statements:
        if s.startswith(("SET LOCAL ROLE", "RESET ROLE", "GRANT ")) or "current_database()" in s:
            continue
        out.append(s)
    return out


def _run(cur: Any, statements: List[str]) -> None:
    for s in _runnable(statements):
        cur.execute(s)


def _catalog(cur: Any, tables: tuple) -> Dict[str, Any]:
    """Columns, constraints, indexes, sequences of ``tables`` and the brokerage views' columns."""
    cur.execute(
        "SELECT c.relname, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
        "coalesce(pg_get_expr(d.adbin, d.adrelid), '') FROM pg_attribute a "
        "JOIN pg_class c ON c.oid = a.attrelid LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE c.oid = ANY(%s::regclass[]) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, 2",
        (list(tables),),
    )
    columns = cur.fetchall()
    cur.execute(
        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = ANY(%s::regclass[]) ORDER BY 1, 2",
        (list(tables),),
    )
    constraints = cur.fetchall()
    cur.execute(
        "SELECT indexrelid::regclass::text, pg_get_indexdef(indexrelid) FROM pg_index "
        "WHERE indrelid = ANY(%s::regclass[]) ORDER BY 1",
        (list(tables),),
    )
    indexes = cur.fetchall()
    cur.execute("SELECT relname FROM pg_class WHERE relkind = 'S' AND relnamespace = 'public'::regnamespace ORDER BY 1")
    sequences = [r[0] for r in cur.fetchall()]
    cur.execute(
        "SELECT table_name, ordinal_position, column_name FROM information_schema.columns "
        "WHERE table_schema = 'brokerage' AND table_name IN "
        "('executions', 'executions_final', 'executions_fly', 'executions_tws', 'trade_fill_splits', "
        "'instance_allocations') ORDER BY 1, 2"
    )
    views = cur.fetchall()
    return {"columns": columns, "constraints": constraints, "indexes": indexes, "sequences": sequences, "views": views}


def _one(cur: Any, sql: str, params: Any = None) -> Any:
    cur.execute(sql, params)
    return cur.fetchone()


@pytest.fixture
def cur(pg_conn: Any):
    ensure_brokerage_schema(pg_conn, log=lambda m: None)
    with pg_conn.cursor() as c:
        c.execute("DROP SCHEMA IF EXISTS brokerage CASCADE")
        c.execute("CREATE SCHEMA brokerage")
        for t in RAW:
            c.execute(f"CREATE VIEW brokerage.{t} AS SELECT * FROM raw_broker.{t}")
        _create_brokerage_views(c, "brokerage", env=True)
        yield c


def _seed(cur: Any) -> Dict[str, int]:
    tpl = _one(cur, "INSERT INTO strategy_template (template_code, display_name) VALUES ('r3_tpl', 'R3') "
                    "RETURNING strategy_template_id")[0]
    struct = _one(cur, "INSERT INTO strategy_structure (name, strategy_template_id) VALUES ('R3', %s) "
                       "RETURNING strategy_structure_id", (tpl,))[0]
    opp = _one(cur, "INSERT INTO strategy_opportunity (name, strategy_structure_id, scope_type) "
                    "VALUES ('R3', %s, 'explicit_symbols') RETURNING strategy_opportunity_id", (struct,))[0]
    a = _one(cur, "INSERT INTO trade (strategy_opportunity_id, account_id, opened_at, label) "
                  "VALUES (%s, %s, now(), 'A') RETURNING trade_id", (opp, ACCT))[0]
    b = _one(cur, "INSERT INTO trade (strategy_opportunity_id, account_id, opened_at) "
                  "VALUES (%s, %s, now()) RETURNING trade_id", (opp, ACCT))[0]
    for exec_id, qty in (("r3.whole", 1.0), ("r3.split", 3.0)):
        cur.execute("INSERT INTO raw_broker.executions_raw_flex (exec_id, account_id, symbol, sec_type, side, quantity, "
                    "source) VALUES (%s, %s, 'QZRT', 'STK', 'BUY', %s, 'flex_trades')", (exec_id, ACCT, qty))
    cur.execute("INSERT INTO trade_execution (account_id, exec_id, trade_id) VALUES (%s, 'r3.whole', %s)", (ACCT, a))
    cur.execute("INSERT INTO trade_execution (account_id, exec_id, trade_id, split_quantity) "
                "VALUES (%s, 'r3.split', %s, 1), (%s, 'r3.split', %s, 2)", (ACCT, a, ACCT, b))
    cur.execute("INSERT INTO strategy_plan (account_id, symbol, structure_label, qty, status, trade_id) "
                "VALUES (%s, 'QZRT', 'Long stock', 1, 'filled', %s)", (ACCT, a))
    cur.execute("INSERT INTO trade_review (trade_id, tags_added_json) VALUES (%s, '[\"late\"]')", (a,))
    cur.execute("INSERT INTO account_execution_instance_allocation (account_id, account_executions_id, "
                "strategy_instance_id, allocated_quantity) VALUES (%s, 1, %s, 1)", (ACCT, a))
    return {"opp": opp, "a": a, "b": b}


def test_reverse_then_forward_ends_where_it_started(cur: Any) -> None:
    ids = _seed(cur)
    fresh = _catalog(cur, TABLES)

    _run(cur, reverse_statements("stg"))
    assert _one(cur, "SELECT relkind FROM pg_class WHERE oid = 'public.strategy_instance'::regclass") == ("r",)
    assert _one(cur, "SELECT to_regclass('public.trade'), to_regclass('brokerage.trade_fill_splits')") == (None, None)
    assert _one(cur, "SELECT tags_added FROM trade_review WHERE strategy_instance_id = %s", (ids["a"],)) == (["late"],)
    # core 0.44.0's env views: IB's trade_id, the attribution as strategy_instance_id
    assert _one(cur, "SELECT strategy_instance_id, trade_id FROM brokerage.executions WHERE exec_id = 'r3.whole'") == (
        ids["a"], None)
    assert _one(cur, "SELECT count(*) FROM brokerage.instance_allocations WHERE exec_id = 'r3.split'") == (2,)
    # core 0.45.0 refuses this database (and has changed nothing when it does)
    with pytest.raises(RuntimeError, match="still a table"):
        refuse_unmigrated_trade_entity(cur)

    _run(cur, forward_statements("stg"))
    assert _catalog(cur, TABLES) == fresh
    # the counts the scripts compare survived both ways (their own DO blocks would have raised)
    assert _one(cur, "SELECT count(*) FROM trade_execution WHERE split_quantity IS NOT NULL") == (2,)
    assert _one(cur, "SELECT trade_id, strategy_instance_id FROM brokerage.executions WHERE exec_id = 'r3.whole'") == (
        ids["a"], ids["a"])
    # db-init on the renamed database: no refusal, nothing changes
    refuse_unmigrated_trade_entity(cur)
    ensure_trade_tables(cur)
    assert _catalog(cur, TABLES) == fresh


def test_a_pod_on_core_044_works_through_the_compatibility_objects(cur: Any) -> None:
    ids = _seed(cur)
    _run(cur, reverse_statements("stg"))
    _run(cur, forward_statements("stg"))
    # create / update / lock / delete a trade through the view (core 0.44.0's SQL)
    new = _one(cur, "INSERT INTO strategy_instance (strategy_opportunity_id, account_id, opened_at, label, updated_at) "
                    "VALUES (%s, %s, now(), 'via view', now()) RETURNING strategy_instance_id", (ids["opp"], ACCT))[0]
    assert _one(cur, "SELECT label FROM trade WHERE trade_id = %s", (new,)) == ("via view",)
    cur.execute("UPDATE strategy_instance SET label = 'renamed', updated_at = now() WHERE strategy_instance_id = %s", (new,))
    assert _one(cur, "SELECT 1 FROM strategy_instance WHERE strategy_instance_id = %s FOR UPDATE", (new,)) == (1,)
    # the whole-fill upsert through the view, twice: insert, then the conflict arm
    upsert = (
        "INSERT INTO strategy_instance_execution (account_id, exec_id, strategy_instance_id) VALUES (%s, %s, %s) "
        "ON CONFLICT (account_id, exec_id) WHERE allocated_quantity IS NULL "
        "DO UPDATE SET strategy_instance_id = EXCLUDED.strategy_instance_id, updated_at = now()"
    )
    cur.execute(upsert, (ACCT, "r3.whole", new))
    assert _one(cur, "SELECT trade_id FROM trade_execution WHERE exec_id = 'r3.whole'") == (new,)
    cur.execute(upsert, (ACCT, "r3.other", new))
    assert _one(cur, "SELECT count(*) FROM trade_execution WHERE trade_id = %s", (new,)) == (2,)
    # splits through the view
    cur.execute("DELETE FROM strategy_instance_execution WHERE account_id = %s AND exec_id = 'r3.split' "
                "AND allocated_quantity IS NOT NULL", (ACCT,))
    cur.execute("INSERT INTO strategy_instance_execution (account_id, exec_id, strategy_instance_id, allocated_quantity) "
                "VALUES (%s, 'r3.split', %s, 3)", (ACCT, new))
    assert _one(cur, "SELECT split_quantity FROM trade_execution WHERE exec_id = 'r3.split'") == (3,)
    assert _one(cur, "SELECT strategy_instance_id, allocated_quantity FROM brokerage.instance_allocations "
                     "WHERE exec_id = 'r3.split'") == (new, 3.0)
    cur.execute("DELETE FROM strategy_instance_execution WHERE exec_id IN ('r3.whole', 'r3.other', 'r3.split')")
    cur.execute("DELETE FROM strategy_instance WHERE strategy_instance_id = %s", (new,))
    assert _one(cur, "SELECT count(*) FROM trade WHERE trade_id = %s", (new,)) == (0,)


def test_core_044_plan_and_review_columns_are_gone(cur: Any) -> None:
    """The writes the window loses: core 0.44.0 names strategy_plan / trade_review columns
    that the rename moved, and those tables have no compatibility view."""
    ids = _seed(cur)
    _run(cur, reverse_statements("stg"))
    _run(cur, forward_statements("stg"))
    cur.execute("SAVEPOINT r3_old")
    for sql in (
        "SELECT p.strategy_instance_id FROM strategy_plan p",
        "SELECT tags_added FROM trade_review",
        "SELECT strategy_instance_id FROM trade_review",
    ):
        with pytest.raises(psycopg2.errors.UndefinedColumn):
            cur.execute(sql)
        cur.execute("ROLLBACK TO SAVEPOINT r3_old")
    assert _one(cur, "SELECT count(*) FROM trade_review WHERE trade_id = %s", (ids["a"],)) == (1,)


def test_the_forward_script_refuses_a_renamed_database(cur: Any) -> None:
    _seed(cur)
    cur.execute("SAVEPOINT r3_again")
    with pytest.raises(psycopg2.errors.RaiseException, match="already exists"):
        _run(cur, forward_statements("stg"))
    cur.execute("ROLLBACK TO SAVEPOINT r3_again")
