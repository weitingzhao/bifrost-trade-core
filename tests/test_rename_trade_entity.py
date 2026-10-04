"""Naming R3 scripts as text: one transaction per env, dry run by default, DEV's owner switch.

The SQL runs on a real database in ``test_rename_trade_entity_db.py`` (and is rehearsed on a
copy of DEV's schema); here: the shape the Owner pastes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bifrost_core.persistence.postgres import rename_trade_entity as r3
from bifrost_core.persistence.postgres import rename_trade_entity_reverse as r3rev

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "db" / "rename_trade_entity.py"


@pytest.mark.parametrize("build", [r3.forward_sql, r3rev.reverse_sql], ids=["forward", "reverse"])
@pytest.mark.parametrize("env", ["dev", "stg", "prod"])
def test_one_transaction_that_rolls_back_unless_committed(build, env: str) -> None:
    sql = build(env)
    body = sql.split("\n", 1)[1]
    assert body.startswith("BEGIN;") and sql.rstrip().endswith("ROLLBACK;")
    assert build(env, commit=True).rstrip().endswith("COMMIT;")
    assert sql.count("BEGIN;") == 1 and "COMMIT;" not in sql and ";;" not in sql
    assert "SET LOCAL lock_timeout = '5s';" in sql
    assert f"current_database() <> 'bifrost_{env}'" in sql


def test_only_dev_switches_to_the_view_owner() -> None:
    dev, stg = r3.forward_sql("dev"), r3.forward_sql("stg")
    assert "IS DISTINCT FROM 'postgres'" in dev and "IS DISTINCT FROM 'bifrost'" in stg
    assert "RESET ROLE" not in stg and "GRANT" not in stg
    # DEV: the drops and the rebuild run as postgres, then the app role gets SELECT back
    assert dev.count("RESET ROLE;") == 2
    first_reset = dev.index("RESET ROLE;")
    assert first_reset < dev.index("DROP VIEW brokerage.executions;") < dev.index("ALTER TABLE public.strategy_instance RENAME")
    grant = dev.index("GRANT SELECT ON brokerage.instance_allocations, brokerage.trade_fill_splits")
    assert dev.index("CREATE OR REPLACE VIEW brokerage.trade_fill_splits") < grant
    assert dev.index("SET LOCAL ROLE bifrost;", grant) < dev.index("SELECT what, before, after")
    rev = r3rev.reverse_sql("dev")
    assert rev.count("RESET ROLE;") == 2 and "GRANT SELECT ON brokerage.instance_allocations, brokerage.executions_tws" in rev


def test_the_forward_steps_are_in_the_packs_order() -> None:
    sql = r3.forward_sql("prod")
    order = [
        "RAISE EXCEPTION 'R3: public.trade already exists",
        "CREATE TEMP TABLE r3_before",
        "DROP VIEW brokerage.instance_allocations;",
        "ALTER TABLE public.strategy_instance RENAME TO trade;",
        "ALTER TABLE public.strategy_instance_execution RENAME TO trade_execution;",
        "ALTER TABLE public.trade_review RENAME COLUMN tags_dropped TO tags_dropped_json;",
        "CREATE VIEW public.strategy_instance AS",
        "CREATE OR REPLACE VIEW brokerage.executions AS",
        "CREATE OR REPLACE VIEW brokerage.instance_allocations AS",
        "SELECT what, before, after",
        "R3: a count changed across the rename",
    ]
    at = [sql.index(s) for s in order]
    assert at == sorted(at)
    assert len([s for s in sql.split(";\n") if "RENAME" in s]) == len(r3.RENAMES) == 27


def test_the_compatibility_view_has_no_notes() -> None:
    """strategy_instance.notes was dropped on 2026-10-03; the view must not list it."""
    view = r3.COMPAT_VIEWS[0]
    assert "notes" not in view and "AS strategy_instance_id" in view
    assert "split_quantity AS allocated_quantity" in r3.COMPAT_VIEWS[1]


def test_the_reverse_undoes_every_rename_backwards() -> None:
    rev = r3rev.reverse_sql("stg")
    lines = [ln for ln in rev.splitlines() if ln.startswith(("ALTER TABLE", "ALTER SEQUENCE", "ALTER INDEX"))]
    assert lines[0] == "ALTER TABLE public.trade_review RENAME COLUMN tags_dropped_json TO tags_dropped;"
    assert lines[-1] == "ALTER TABLE public.trade RENAME TO strategy_instance;"
    assert len(lines) == len(r3.RENAMES)
    # the compatibility views go before the tables take their names back
    assert rev.index("DROP VIEW IF EXISTS public.strategy_instance;") < rev.index(lines[-1])
    # core 0.44.0's env views, embedded: attribution as strategy_instance_id over the old table
    assert "LEFT JOIN public.strategy_instance_execution sie" in rev
    assert "CREATE OR REPLACE VIEW brokerage.instance_allocations AS" in rev
    assert "trade_fill_splits" not in rev.split("CREATE OR REPLACE VIEW brokerage.executions AS", 1)[1]


def test_unknown_env_is_refused() -> None:
    with pytest.raises(ValueError, match="env must be one of"):
        r3.forward_sql("qa")


def test_the_script_prints_the_sql(tmp_path: Path) -> None:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--env", "stg", "--reverse"], capture_output=True, text=True, check=True
    ).stdout
    assert out == r3rev.reverse_sql("stg")
    out = subprocess.run([sys.executable, str(SCRIPT), "--env", "prod", "--commit"], capture_output=True, text=True,
                         check=True).stdout
    assert out == r3.forward_sql("prod", commit=True)
