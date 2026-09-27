"""Wave 13 against real Postgres: a database with the pre-split leftovers converges on the DDL.

Marked `db`: `make test` skips it, `make test-all` with PGHOST runs it. Every
test runs in the fixture's transaction and is rolled back at teardown.
"""

from __future__ import annotations

from typing import Any, Dict

import psycopg2
import pytest

from bifrost_core.persistence.postgres.wave13_migrations import (
    migrate_wave13_reconcile_legacy_schema,
    wave13_statements,
)

pytestmark = pytest.mark.db

_TABLES = (
    "settings",
    "strategy_allocation",
    "strategy_allocation_opportunity",
    "preference_market_streams_symbol_order",
    "watchlist",
)

# The live databases as read 2026-09-27, rebuilt on top of the DDL.
_LEGACY = (
    "ALTER SEQUENCE strategy_allocation_strategy_allocation_id_seq RENAME TO strategy_portfolio_strategy_portfolio_id_seq",
    "ALTER TABLE strategy_allocation RENAME CONSTRAINT strategy_allocation_pkey TO strategy_portfolio_pkey",
    "ALTER TABLE strategy_allocation RENAME CONSTRAINT strategy_allocation_gate_safety_strategy_id_fkey "
    "TO strategy_portfolio_gate_safety_strategy_id_fkey",
    "ALTER TABLE strategy_allocation_opportunity RENAME CONSTRAINT strategy_allocation_opportunity_pkey "
    "TO strategy_portfolio_opportunity_pkey",
    "ALTER TABLE preference_market_streams_symbol_order RENAME CONSTRAINT preference_market_streams_symbol_order_pkey "
    "TO market_streams_symbol_order_pkey",
    "ALTER INDEX strategy_allocation_opportunity_opportunity_id RENAME TO strategy_portfolio_opportunity_opportunity_id",
    "CREATE INDEX watchlist_contract_key ON watchlist (contract_key)",
    "ALTER TABLE strategy_allocation_opportunity DROP CONSTRAINT strategy_allocation_opportunity_strategy_opportunity_id_fkey",
    "ALTER TABLE settings ALTER COLUMN flex_default_range_days DROP NOT NULL",
    "ALTER TABLE settings ALTER COLUMN flex_init_range_days DROP NOT NULL",
    "UPDATE settings SET flex_default_range_days = NULL",
    "ALTER TABLE settings ADD COLUMN ib_primary_account_id text, ADD COLUMN stream_primary_account_id text",
)


def _catalog(cur: Any) -> Dict[str, Any]:
    """Columns, constraints, indexes and sequences of the tables Wave 13 touches."""
    cur.execute(
        """
        SELECT table_name, column_name, data_type, is_nullable, coalesce(column_default, '')
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ANY(%s)
        ORDER BY 1, 2
        """,
        (list(_TABLES),),
    )
    columns = cur.fetchall()
    cur.execute(
        """
        SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid), convalidated
        FROM pg_constraint
        WHERE conrelid = ANY(%s::regclass[])
        ORDER BY 1, 2
        """,
        ([f"public.{t}" for t in _TABLES],),
    )
    constraints = cur.fetchall()
    cur.execute(
        "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = ANY(%s) ORDER BY 1, 2",
        (list(_TABLES),),
    )
    indexes = cur.fetchall()
    cur.execute(
        "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' AND sequencename LIKE 'strategy_%%' ORDER BY 1"
    )
    sequences = cur.fetchall()
    return {"columns": columns, "constraints": constraints, "indexes": indexes, "sequences": sequences}


def _legacy(cur: Any) -> None:
    for stmt in _LEGACY:
        cur.execute(stmt)


def test_legacy_schema_converges_on_the_ddl(pg_conn):
    with pg_conn.cursor() as cur:
        declared = _catalog(cur)
        _legacy(cur)
        assert _catalog(cur) != declared
        migrate_wave13_reconcile_legacy_schema(cur)
        assert _catalog(cur) == declared
        cur.execute("SELECT flex_default_range_days, flex_init_range_days FROM settings WHERE id = 1")
        assert cur.fetchone() == (30, 360)


def test_wave13_is_a_no_op_on_a_converged_schema(pg_conn):
    with pg_conn.cursor() as cur:
        declared = _catalog(cur)
        migrate_wave13_reconcile_legacy_schema(cur)
        migrate_wave13_reconcile_legacy_schema(cur)
        assert _catalog(cur) == declared


def test_a_duplicate_left_by_an_earlier_refresh_is_dropped(pg_conn):
    # Pre-0.24 core refreshing a legacy DB creates the declared index next to the legacy one.
    with pg_conn.cursor() as cur:
        declared = _catalog(cur)
        _legacy(cur)
        cur.execute(
            "CREATE INDEX strategy_allocation_opportunity_opportunity_id "
            "ON strategy_allocation_opportunity (strategy_opportunity_id)"
        )
        migrate_wave13_reconcile_legacy_schema(cur)
        assert _catalog(cur) == declared


def test_orphans_leave_the_foreign_key_not_valid_rather_than_failing(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE strategy_allocation_opportunity "
            "DROP CONSTRAINT strategy_allocation_opportunity_strategy_opportunity_id_fkey"
        )
        cur.execute("INSERT INTO strategy_allocation (name) VALUES ('wave13 orphan') RETURNING strategy_allocation_id")
        allocation_id = cur.fetchone()[0]
        cur.execute("SELECT coalesce(max(strategy_opportunity_id), 0) + 1000 FROM strategy_opportunity")
        missing = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO strategy_allocation_opportunity (strategy_allocation_id, strategy_opportunity_id) VALUES (%s, %s)",
            (allocation_id, missing),
        )
        migrate_wave13_reconcile_legacy_schema(cur)
        cur.execute(
            """
            SELECT convalidated FROM pg_constraint
            WHERE conname = 'strategy_allocation_opportunity_strategy_opportunity_id_fkey'
            """
        )
        assert cur.fetchone() == (False,)


def test_a_non_owner_can_run_it_on_a_converged_schema(pg_conn):
    with pg_conn.cursor() as cur:
        try:
            cur.execute("SAVEPOINT role_probe")
            cur.execute("CREATE ROLE wave13_reader NOLOGIN")
        except psycopg2.Error:
            cur.execute("ROLLBACK TO SAVEPOINT role_probe")
            pytest.skip("test role cannot create roles")
        cur.execute("GRANT USAGE ON SCHEMA public TO wave13_reader")
        cur.execute("SET LOCAL ROLE wave13_reader")
        for stmt in wave13_statements():
            cur.execute(stmt)
        cur.execute("RESET ROLE")
