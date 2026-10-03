"""Wave 14 against real Postgres: databases built before core 0.41.0 converge on the DDL.

Marked `db` (``make test-db``). Every test runs in the fixture's transaction and is rolled
back at teardown. Names and rows are made up.
"""

from __future__ import annotations

from typing import Any, Dict

import psycopg2
import pytest

from bifrost_core.persistence.postgres.wave14_migrations import (
    CATEGORY_NAME_UQ,
    OPPORTUNITY_SCOPE_TYPE_CK,
    PLAN_FILLED_INSTANCE_CK,
    migrate_wave14_trade_invariants,
    wave14_statements,
)

pytestmark = pytest.mark.db

_TABLES = (
    "strategy_plan",
    "trade_review",
    "strategy_opportunity",
    "preference_position_categories",
    "preference_position_category_tags",
    "watchlist",
)

# bifrost_{dev,stg,prod} as read 2026-10-03, rebuilt on top of the DDL.
_LEGACY = (
    "ALTER TABLE strategy_plan DROP CONSTRAINT strategy_plan_strategy_instance_id_fkey, "
    "ADD CONSTRAINT strategy_plan_strategy_instance_id_fkey FOREIGN KEY (strategy_instance_id) "
    "REFERENCES strategy_instance(strategy_instance_id) ON DELETE SET NULL",
    f"ALTER TABLE strategy_plan DROP CONSTRAINT {PLAN_FILLED_INSTANCE_CK}",
    "ALTER TABLE trade_review DROP CONSTRAINT trade_review_strategy_instance_id_fkey, "
    "ADD CONSTRAINT trade_review_strategy_instance_id_fkey FOREIGN KEY (strategy_instance_id) "
    "REFERENCES strategy_instance(strategy_instance_id) ON DELETE CASCADE",
    f"ALTER TABLE strategy_opportunity DROP CONSTRAINT {OPPORTUNITY_SCOPE_TYPE_CK}",
    "ALTER TABLE preference_position_category_tags ALTER COLUMN category_id TYPE integer",
    "ALTER TABLE watchlist ALTER COLUMN category_id TYPE integer",
    f"ALTER TABLE preference_position_categories DROP CONSTRAINT {CATEGORY_NAME_UQ}",
)


def _catalog(cur: Any) -> Dict[str, Any]:
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
        "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = ANY(%s) "
        "ORDER BY 1, 2",
        (list(_TABLES),),
    )
    return {"columns": columns, "constraints": constraints, "indexes": cur.fetchall()}


def _legacy(cur: Any) -> None:
    for stmt in _LEGACY:
        cur.execute(stmt)


def _validated(cur: Any, conname: str) -> Any:
    cur.execute("SELECT convalidated FROM pg_constraint WHERE conname = %s", (conname,))
    row = cur.fetchone()
    return None if row is None else row[0]


def test_the_ddl_declares_what_the_plan_approved(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT conname, confdeltype FROM pg_constraint WHERE conname IN "
            "('strategy_plan_strategy_instance_id_fkey', 'trade_review_strategy_instance_id_fkey') ORDER BY 1"
        )
        assert cur.fetchall() == [("strategy_plan_strategy_instance_id_fkey", "r"), ("trade_review_strategy_instance_id_fkey", "r")]
        for name in (PLAN_FILLED_INSTANCE_CK, OPPORTUNITY_SCOPE_TYPE_CK, CATEGORY_NAME_UQ):
            assert _validated(cur, name) is True
        cur.execute(
            "SELECT table_name, data_type FROM information_schema.columns WHERE table_schema = 'public' "
            "AND column_name = 'category_id' AND table_name IN ('preference_position_category_tags', 'watchlist') ORDER BY 1"
        )
        assert cur.fetchall() == [("preference_position_category_tags", "bigint"), ("watchlist", "bigint")]


def test_legacy_schema_converges_on_the_ddl(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        declared = _catalog(cur)
        _legacy(cur)
        assert _catalog(cur) != declared
        migrate_wave14_trade_invariants(cur)
        assert _catalog(cur) == declared


def test_wave14_is_a_no_op_on_a_converged_schema(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        declared = _catalog(cur)
        migrate_wave14_trade_invariants(cur)
        migrate_wave14_trade_invariants(cur)
        assert _catalog(cur) == declared


def test_rows_that_break_a_check_leave_it_not_valid_until_they_are_fixed(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        _legacy(cur)
        cur.execute(
            "INSERT INTO strategy_plan (account_id, symbol, structure_label, qty, status) "
            "VALUES ('U0000001', 'ZZZQ', 'Put', 1, 'filled') RETURNING strategy_plan_id"
        )
        plan_id = cur.fetchone()[0]
        cur.execute("INSERT INTO strategy_structure (name) VALUES ('w14') RETURNING strategy_structure_id")
        cur.execute(
            "INSERT INTO strategy_opportunity (name, strategy_structure_id, scope_type) "
            "VALUES ('w14', %s, 'symbols') RETURNING strategy_opportunity_id",
            (cur.fetchone()[0],),
        )
        opp_id = cur.fetchone()[0]
        migrate_wave14_trade_invariants(cur)
        assert _validated(cur, PLAN_FILLED_INSTANCE_CK) is False
        assert _validated(cur, OPPORTUNITY_SCOPE_TYPE_CK) is False
        # NOT VALID still checks new writes.
        cur.execute("SAVEPOINT w14")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE strategy_opportunity SET scope_type = 'nonsense' WHERE strategy_opportunity_id = %s", (opp_id,))
        cur.execute("ROLLBACK TO SAVEPOINT w14")
        cur.execute("UPDATE strategy_plan SET status = 'cancelled' WHERE strategy_plan_id = %s", (plan_id,))
        cur.execute("UPDATE strategy_opportunity SET scope_type = 'explicit_symbols' WHERE strategy_opportunity_id = %s", (opp_id,))
        migrate_wave14_trade_invariants(cur)
        assert _validated(cur, PLAN_FILLED_INSTANCE_CK) is True
        assert _validated(cur, OPPORTUNITY_SCOPE_TYPE_CK) is True


def test_a_duplicated_category_name_skips_the_unique_until_it_is_resolved(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        _legacy(cur)
        cur.execute("INSERT INTO preference_position_categories (name) VALUES ('W14 Twin'), ('W14 Twin') RETURNING id")
        twin = cur.fetchall()[-1][0]
        migrate_wave14_trade_invariants(cur)
        assert _validated(cur, CATEGORY_NAME_UQ) is None
        cur.execute("DELETE FROM preference_position_categories WHERE id = %s", (twin,))
        migrate_wave14_trade_invariants(cur)
        assert _validated(cur, CATEGORY_NAME_UQ) is True


def test_the_type_change_keeps_every_value(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO preference_position_categories (name) VALUES ('W14 Kept') RETURNING id")
        cat = cur.fetchone()[0]
        _legacy(cur)
        cur.execute(
            "INSERT INTO preference_position_category_tags (account_id, contract_key, category_id) "
            "VALUES ('U0000001', 'ZZZQ|STK|||', %s)",
            (cat,),
        )
        cur.execute("INSERT INTO watchlist (contract_key, symbol, sec_type, category_id) VALUES ('ZZZQ|STK|||', 'ZZZQ', 'STK', %s)", (cat,))
        migrate_wave14_trade_invariants(cur)
        cur.execute("SELECT category_id FROM preference_position_category_tags WHERE contract_key = 'ZZZQ|STK|||'")
        assert cur.fetchone() == (cat,)
        cur.execute("SELECT category_id FROM watchlist WHERE contract_key = 'ZZZQ|STK|||'")
        assert cur.fetchone() == (cat,)


def test_a_non_owner_can_run_it_on_a_converged_schema(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        try:
            cur.execute("SAVEPOINT role_probe")
            cur.execute("CREATE ROLE wave14_reader NOLOGIN")
        except psycopg2.Error:
            cur.execute("ROLLBACK TO SAVEPOINT role_probe")
            pytest.skip("test role cannot create roles")
        cur.execute("GRANT USAGE ON SCHEMA public TO wave14_reader")
        cur.execute("SET LOCAL ROLE wave14_reader")
        for stmt in wave14_statements():
            cur.execute(stmt)
        cur.execute("RESET ROLE")
