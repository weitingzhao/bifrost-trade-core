"""Wave 14 (core 0.41.0): rules that lived only in code move into the schema.

Owner-approved 2026-10-03 (REQUEST-td-ddl-batch-plans-2026-10-03.md). Every count below was
re-read on bifrost_{dev,stg,prod} that day: 0 rows violate any of these.

TD-43 (trade lifecycle):
- ``strategy_plan.strategy_instance_id`` FK: ON DELETE SET NULL -> RESTRICT. A filled plan
  can no longer lose its instance; deleting that instance is refused (409 in core).
- CHECK ``strategy_plan_filled_instance_ck``: ``(status = 'filled') = (strategy_instance_id IS NOT NULL)``.
  ``link_fill`` is the one writer of that column and sets both together.
- ``trade_review.strategy_instance_id`` FK: ON DELETE CASCADE -> RESTRICT ("never deletes" is now true).
  ``strategy_plan.filled_at`` stays until the next wave: core stops writing it and reads the
  instance's ``opened_at`` instead. (core 0.43.0 stops naming it anywhere; the column is dropped
  by an Owner db-step after that release, never by db-init.)

TD-56 (position categories; table and ``id`` PK unchanged, Owner 2026-10-03):
- ``preference_position_category_tags.category_id`` and ``watchlist.category_id``: int4 -> bigint,
  the type of the key they reference.
- UNIQUE ``preference_position_categories_name_uq (name)``: the name is the key the symbol
  order is stored under, so two categories may not share it.

TD-71: CHECK ``strategy_opportunity_scope_type_ck``: ``scope_type`` is NULL, 'watchlist_stk'
or 'explicit_symbols'.

Each step reads the catalog first and changes nothing on a database that already matches,
so a non-owner role running ``_ensure_tables`` is not broken by it. A CHECK is added NOT VALID
and then validated; rows that break it leave it NOT VALID with a WARNING (new writes are
still checked) instead of failing the schema refresh, and the next refresh validates it
again. The UNIQUE is skipped with a WARNING while a name is duplicated. The deletion of the
three orphan 'Option Pool' symbol-order rows is not here: it removes data, so it is an
Owner step (infra scripts/release/db-steps.d/).
"""

from __future__ import annotations

from typing import Any, List, Tuple

# (table, constraint, column, referenced table, referenced column). Recreated ON DELETE RESTRICT
# when the stored rule is anything else (confdeltype 'r' = RESTRICT).
_RESTRICT_FKS: Tuple[Tuple[str, str, str, str, str], ...] = (
    (
        "strategy_plan",
        "strategy_plan_strategy_instance_id_fkey",
        "strategy_instance_id",
        "strategy_instance",
        "strategy_instance_id",
    ),
    (
        "trade_review",
        "trade_review_strategy_instance_id_fkey",
        "strategy_instance_id",
        "strategy_instance",
        "strategy_instance_id",
    ),
)

# (table, constraint, CHECK expression).
PLAN_FILLED_INSTANCE_CK = "strategy_plan_filled_instance_ck"
OPPORTUNITY_SCOPE_TYPE_CK = "strategy_opportunity_scope_type_ck"
_CHECKS: Tuple[Tuple[str, str, str], ...] = (
    ("strategy_plan", PLAN_FILLED_INSTANCE_CK, "(status = 'filled') = (strategy_instance_id IS NOT NULL)"),
    (
        "strategy_opportunity",
        OPPORTUNITY_SCOPE_TYPE_CK,
        "scope_type IS NULL OR scope_type IN ('watchlist_stk', 'explicit_symbols')",
    ),
)

# (table, column): int4 -> bigint.
_BIGINT_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("preference_position_category_tags", "category_id"),
    ("watchlist", "category_id"),
)

CATEGORY_NAME_UQ = "preference_position_categories_name_uq"


def wave14_statements() -> List[str]:
    """The migration as SQL, in order: what _ensure_tables runs, and what an operator can apply by hand."""
    stmts: List[str] = []
    for table, conname, col, ref_table, ref_col in _RESTRICT_FKS:
        stmts.append(
            f"""
            DO $w14$
            BEGIN
              IF to_regclass('public.{table}') IS NOT NULL
                 AND EXISTS (SELECT 1 FROM pg_constraint
                             WHERE conrelid = 'public.{table}'::regclass AND conname = '{conname}'
                               AND contype = 'f' AND confdeltype <> 'r') THEN
                ALTER TABLE public.{table}
                  DROP CONSTRAINT {conname},
                  ADD CONSTRAINT {conname} FOREIGN KEY ({col})
                    REFERENCES public.{ref_table}({ref_col}) ON DELETE RESTRICT;
              END IF;
            END $w14$;
            """
        )
    for table, conname, expr in _CHECKS:
        stmts.append(
            f"""
            DO $w14$
            BEGIN
              IF to_regclass('public.{table}') IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM pg_constraint
                                 WHERE conrelid = 'public.{table}'::regclass AND conname = '{conname}') THEN
                ALTER TABLE public.{table} ADD CONSTRAINT {conname} CHECK ({expr}) NOT VALID;
              END IF;
              IF to_regclass('public.{table}') IS NOT NULL
                 AND EXISTS (SELECT 1 FROM pg_constraint
                             WHERE conrelid = 'public.{table}'::regclass AND conname = '{conname}'
                               AND NOT convalidated) THEN
                BEGIN
                  ALTER TABLE public.{table} VALIDATE CONSTRAINT {conname};
                EXCEPTION WHEN check_violation THEN
                  RAISE WARNING '{conname} left NOT VALID: rows of {table} break it (new writes are checked)';
                END;
              END IF;
            END $w14$;
            """
        )
    for table, col in _BIGINT_COLUMNS:
        stmts.append(
            f"""
            DO $w14$
            BEGIN
              IF EXISTS (SELECT 1 FROM information_schema.columns
                         WHERE table_schema = 'public' AND table_name = '{table}'
                           AND column_name = '{col}' AND data_type = 'integer') THEN
                ALTER TABLE public.{table} ALTER COLUMN {col} TYPE bigint;
              END IF;
            END $w14$;
            """
        )
    stmts.append(
        f"""
        DO $w14$
        BEGIN
          IF to_regclass('public.preference_position_categories') IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM pg_constraint
                             WHERE conrelid = 'public.preference_position_categories'::regclass
                               AND conname = '{CATEGORY_NAME_UQ}') THEN
            BEGIN
              ALTER TABLE public.preference_position_categories
                ADD CONSTRAINT {CATEGORY_NAME_UQ} UNIQUE (name);
            EXCEPTION WHEN unique_violation THEN
              RAISE WARNING '{CATEGORY_NAME_UQ} not added: two position categories share a name';
            END;
          END IF;
        END $w14$;
        """
    )
    return stmts


def migrate_wave14_trade_invariants(cur: Any) -> None:
    """Idempotent Wave 14 migration. Run it once the strategy, review, category and watchlist tables exist."""
    for stmt in wave14_statements():
        cur.execute(stmt)
