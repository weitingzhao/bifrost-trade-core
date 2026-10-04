"""Naming R4's drop step against real Postgres (core 0.47.0).

The fresh schema (core 0.47.0's ``_ensure_tables`` and env views) gets R3's one-version objects
back through ``reverse_statements`` (as the envs have them today), two frozen split rows that
are in ``trade_execution``, then the drop: the four objects go, the env views are rebuilt without
``strategy_instance_id`` (db-init leaves them alone in the real envs), every count stays, and
db-init afterwards creates none of them again. The guards refuse a third frozen row, an
unmatched one, a table where a view should be and a view outside the step that depends on the
views it rebuilds. The CSV export / restore round-trips the rows.

The database-name and owner guards and the role switch are left out (the throwaway database is
not ``bifrost_<env>`` and runs as one superuser); the full SQL runs in the rehearsal on PROD's
schema (infra db-steps 2026-10-08-r4-drop-compat). Marked ``db``; everything is rolled back.
Accounts, symbols and exec ids are made up.
"""

from __future__ import annotations

import io
from typing import Any, Dict, List

import psycopg2
import pytest

from bifrost_core.persistence.postgres import drop_trade_compat as r4
from bifrost_core.persistence.postgres.brokerage_ddl import _create_brokerage_views, ensure_brokerage_schema
from bifrost_core.persistence.postgres.trade_ddl import ensure_trade_tables

pytestmark = pytest.mark.db

ACCT = "U0000001"
RAW = ("executions_raw_flex", "executions_raw_tws", "executions_raw_journal")


def _runnable(statements: List[str]) -> List[str]:
    return [
        s for s in statements
        if not s.startswith("SET LOCAL ROLE") and "current_database() <> " not in s and "relowner" not in s
    ]


def _run(cur: Any, statements: List[str]) -> None:
    for s in _runnable(statements):
        cur.execute(s)


def _one(cur: Any, sql: str, params: Any = None) -> Any:
    cur.execute(sql, params)
    return cur.fetchone()


def _present(cur: Any) -> Dict[str, bool]:
    cur.execute("SELECT o, to_regclass(o) IS NOT NULL FROM unnest(%s::text[]) o", (list(r4.DROPPED),))
    return dict(cur.fetchall())


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
    """Two trades, a whole fill, a fill split 1 + 2, R3's objects and the two frozen rows."""
    tpl = _one(cur, "INSERT INTO strategy_template (template_code, display_name) VALUES ('r4_tpl', 'R4') "
                    "RETURNING strategy_template_id")[0]
    struct = _one(cur, "INSERT INTO strategy_structure (name, strategy_template_id) VALUES ('R4', %s) "
                       "RETURNING strategy_structure_id", (tpl,))[0]
    opp = _one(cur, "INSERT INTO strategy_opportunity (name, strategy_structure_id, scope_type) "
                    "VALUES ('R4', %s, 'explicit_symbols') RETURNING strategy_opportunity_id", (struct,))[0]
    a = _one(cur, "INSERT INTO trade (strategy_opportunity_id, account_id, opened_at) VALUES (%s, %s, now()) "
                  "RETURNING trade_id", (opp, ACCT))[0]
    b = _one(cur, "INSERT INTO trade (strategy_opportunity_id, account_id, opened_at) VALUES (%s, %s, now()) "
                  "RETURNING trade_id", (opp, ACCT))[0]
    ids = {"a": a, "b": b}
    for exec_id, qty in (("r4.whole", 1.0), ("r4.split", 3.0)):
        ids[exec_id] = _one(cur, "INSERT INTO raw_broker.executions_raw_flex (exec_id, account_id, symbol, sec_type, "
                                 "side, quantity, source) VALUES (%s, %s, 'QZRV', 'STK', 'BUY', %s, 'flex_trades') "
                                 "RETURNING executions_raw_flex_id", (exec_id, ACCT, qty))[0]
    cur.execute("INSERT INTO trade_execution (account_id, exec_id, trade_id) VALUES (%s, 'r4.whole', %s)", (ACCT, a))
    cur.execute("INSERT INTO trade_execution (account_id, exec_id, trade_id, split_quantity) "
                "VALUES (%s, 'r4.split', %s, 1), (%s, 'r4.split', %s, 2)", (ACCT, a, ACCT, b))
    _run(cur, r4.reverse_statements("stg"))  # R3's objects, as the envs have them before R4
    cur.execute("INSERT INTO public.account_execution_instance_allocation (account_id, account_executions_id, "
                "strategy_instance_id, allocated_quantity) VALUES (%s, %s, %s, 1), (%s, %s, %s, 2)",
                (ACCT, ids["r4.split"], a, ACCT, ids["r4.split"], b))
    return ids


def test_the_drop_removes_the_four_objects_and_keeps_every_row(cur: Any) -> None:
    ids = _seed(cur)
    assert _present(cur) == {o: True for o in r4.DROPPED}
    assert _one(cur, "SELECT count(*) FROM strategy_instance") == (2,)  # the R3 views read through
    buf = io.StringIO()
    cur.copy_expert(r4.EXPORT_SQL, buf)
    csv = buf.getvalue()
    assert csv.count("\n") == 1 + r4.LEGACY_ROWS

    _run(cur, r4.forward_statements("stg"))  # its own report RAISEs on a changed count
    assert _present(cur) == {o: False for o in r4.DROPPED}
    assert _one(cur, "SELECT count(*) FROM trade_execution") == (3,)
    assert _one(cur, "SELECT trade_id FROM brokerage.executions WHERE exec_id = 'r4.whole'") == (ids["a"],)
    # db-init afterwards (core 0.47.0) makes none of them again; a second run is a no-op
    ensure_trade_tables(cur)
    _create_brokerage_views(cur, "brokerage", env=True)
    assert _present(cur) == {o: False for o in r4.DROPPED}
    _run(cur, r4.forward_statements("stg"))

    # the way back: the objects, then the rows from the CSV
    _run(cur, r4.reverse_statements("stg"))
    assert _present(cur) == {o: True for o in r4.DROPPED}
    cur.copy_expert(r4.RESTORE_SQL, io.StringIO(csv))
    cur.execute(r4.SEQUENCE_SQL)
    back = io.StringIO()
    cur.copy_expert(r4.EXPORT_SQL, back)
    assert back.getvalue() == csv
    nxt = _one(cur, "INSERT INTO public.account_execution_instance_allocation (account_id, account_executions_id, "
                    "strategy_instance_id, allocated_quantity) VALUES (%s, 7, %s, 1) "
                    "RETURNING account_execution_instance_allocation_id", (ACCT, ids["a"]))[0]
    assert nxt > max(int(line.split(",")[0]) for line in csv.splitlines()[1:])


def _compat_columns(cur: Any) -> List[str]:
    cur.execute("SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables t "
                "USING (table_schema, table_name) WHERE c.table_schema = 'brokerage' "
                "AND c.column_name = 'strategy_instance_id' AND t.table_type = 'VIEW' "
                "AND c.table_name NOT LIKE 'executions_raw_%' ORDER BY 1")  # here the raw tables are views
    return [r[0] for r in cur.fetchall()]


def test_it_rebuilds_the_env_views_db_init_leaves_alone(cur: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """dev / stg / prod db-init skips its FDW step, so the env views are R3's (core 0.45.0) when
    the step runs: it rebuilds them as core 0.47.0 does, in the same transaction."""
    from bifrost_core.persistence.postgres import brokerage_views

    _seed(cur)
    with monkeypatch.context() as m:  # core 0.45.0's env view output: the one-version alias
        m.setitem(brokerage_views._ENV_RENAMED, "strategy_instance_id", "te.trade_id, te.trade_id AS strategy_instance_id")
        m.setattr(brokerage_views, "RETIRED_ENV_VIEWS", ())
        brokerage_views._create_brokerage_views(cur, "brokerage", env=True)
    cur.execute(r4.R3_ENV_VIEW)
    assert _compat_columns(cur) == ["executions", "executions_final", "executions_fly", "executions_tws",
                                    "instance_allocations"]
    _run(cur, r4.forward_statements("stg"))
    assert _compat_columns(cur) == []
    assert _present(cur) == {o: False for o in r4.DROPPED}
    assert _one(cur, "SELECT count(*) FROM brokerage.trade_fill_splits WHERE exec_id = 'r4.split'") == (2,)


def test_it_refuses_when_another_view_depends_on_the_env_views(cur: Any) -> None:
    _seed(cur)
    cur.execute("SAVEPOINT r4_dependent")
    cur.execute("CREATE VIEW public.someones_report AS SELECT account_id, count(*) FROM brokerage.executions GROUP BY 1")
    with pytest.raises(psycopg2.errors.RaiseException, match="someones_report"):
        _run(cur, r4.forward_statements("stg"))
    cur.execute("ROLLBACK TO SAVEPOINT r4_dependent")
    assert _present(cur)["public.strategy_instance"]


@pytest.mark.parametrize("extra", ["third", "unmatched"])
def test_it_refuses_a_frozen_table_it_does_not_expect(cur: Any, extra: str) -> None:
    ids = _seed(cur)
    cur.execute("SAVEPOINT r4_rows")
    if extra == "third":
        cur.execute("INSERT INTO public.account_execution_instance_allocation (account_id, account_executions_id, "
                    "strategy_instance_id, allocated_quantity) VALUES (%s, %s, %s, 5)", (ACCT, ids["r4.whole"], ids["a"]))
        match = "has 3 rows, expected 2"
    else:
        cur.execute("UPDATE public.account_execution_instance_allocation SET allocated_quantity = 9 "
                    "WHERE strategy_instance_id = %s", (ids["b"],))
        match = "is not in trade_execution"
    with pytest.raises(psycopg2.errors.RaiseException, match=match):
        _run(cur, r4.forward_statements("stg"))
    cur.execute("ROLLBACK TO SAVEPOINT r4_rows")
    assert _present(cur) == {o: True for o in r4.DROPPED}


def test_a_table_where_a_view_should_be_is_refused(cur: Any) -> None:
    _seed(cur)
    cur.execute("SAVEPOINT r4_table")
    cur.execute("DROP VIEW public.strategy_instance")
    cur.execute("CREATE TABLE public.strategy_instance (strategy_instance_id bigint)")
    with pytest.raises(psycopg2.errors.RaiseException, match="is not a view"):
        _run(cur, r4.forward_statements("stg"))
    cur.execute("ROLLBACK TO SAVEPOINT r4_table")
