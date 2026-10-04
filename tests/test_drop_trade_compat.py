"""Naming R4's drop step, as text (core 0.47.0): one transaction per env, guards first."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bifrost_core.persistence.postgres import drop_trade_compat as r4

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("env", ["dev", "stg", "prod"])
def test_one_transaction_that_rolls_back_unless_committed(env: str) -> None:
    dry = r4.forward_sql(env)
    assert dry.count("BEGIN;") == 1 and dry.rstrip().endswith("ROLLBACK;")
    assert r4.forward_sql(env, commit=True).rstrip().endswith("COMMIT;")
    assert f"current_database() <> 'bifrost_{env}'" in dry
    assert "SET LOCAL ROLE bifrost" in dry and "SET LOCAL lock_timeout = '5s'" in dry


def test_guards_come_before_the_first_drop() -> None:
    sql = r4.forward_sql("prod")
    first_drop = sql.index("DROP VIEW IF EXISTS public.strategy_instance_execution")
    for guard in ("FDW tables are missing", "is not a view", "not owned by bifrost", "another view depends",
                  f"<> {r4.LEGACY_ROWS} THEN", "is not in trade_execution"):
        assert sql.index(guard) < first_drop, guard
    assert sql.index("CREATE TEMP TABLE r4_before") < first_drop


def test_it_drops_the_four_objects_and_rebuilds_the_env_views() -> None:
    sql = r4.forward_sql("stg")
    drops = [line for line in sql.splitlines() if line.startswith("DROP ")]
    assert drops == [
        "DROP TABLE IF EXISTS pg_temp.r4_before;",
        "DROP VIEW IF EXISTS public.strategy_instance_execution;",
        "DROP VIEW IF EXISTS public.strategy_instance;",
        "DROP VIEW IF EXISTS brokerage.instance_allocations;",
        "DROP VIEW IF EXISTS brokerage.executions_tws CASCADE;",
        "DROP VIEW IF EXISTS brokerage.trade_fill_splits CASCADE;",
        "DROP VIEW IF EXISTS brokerage.executions_fly CASCADE;",
        "DROP VIEW IF EXISTS brokerage.executions_final CASCADE;",
        "DROP VIEW IF EXISTS brokerage.executions CASCADE;",
        "DROP TABLE IF EXISTS public.account_execution_instance_allocation;",
    ]
    created = [line.split(" AS")[0] for line in sql.splitlines() if line.startswith("CREATE OR REPLACE VIEW")]
    assert created == [f"CREATE OR REPLACE VIEW {v}" for v in (
        "brokerage.executions", "brokerage.executions_final", "brokerage.executions_fly",
        "brokerage.executions_tws", "brokerage.trade_fill_splits")]
    # the views are core 0.47.0's: no strategy_instance_id output column, no instance_allocations
    assert "AS strategy_instance_id" not in sql.split("CREATE TEMP TABLE r4_before")[1].split("DROP TABLE IF EXISTS public.")[0]
    assert "CREATE OR REPLACE VIEW brokerage.instance_allocations" not in sql


def test_the_reverse_puts_back_the_r3_shapes() -> None:
    sql = r4.reverse_sql("dev", commit=True)
    assert "trade_id AS strategy_instance_id" in sql and "split_quantity AS allocated_quantity" in sql
    assert "quantity AS allocated_quantity, exec_id\n  FROM brokerage.trade_fill_splits" in sql
    assert "CREATE TABLE IF NOT EXISTS public.account_execution_instance_allocation" in sql
    assert sql.rstrip().endswith("COMMIT;")


def test_an_unknown_env_is_refused() -> None:
    with pytest.raises(ValueError, match="env must be one of"):
        r4.forward_sql("qa")


def test_the_script_prints_the_same_sql() -> None:
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts/db/drop_trade_compat.py"), "--env", "prod", "--commit"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert out == r4.forward_sql("prod", commit=True)
    export = subprocess.run(
        [sys.executable, str(ROOT / "scripts/db/drop_trade_compat.py"), "--export-sql"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert export.strip() == r4.EXPORT_SQL and "TO STDOUT" in export
