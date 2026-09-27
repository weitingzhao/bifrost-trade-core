"""Wave 13: bring pre-split schema leftovers in line with the declared DDL.

bifrost_{dev,stg,prod} were built before the repo split. They kept objects that
`CREATE ... IF NOT EXISTS` can never reach (read 2026-09-27, identical in all three):

- strategy_allocation / strategy_allocation_opportunity still carry the
  strategy_portfolio_* names of the table they were renamed from: the sequence,
  both primary keys, the gate FK and the opportunity index. The DDL's
  `CREATE INDEX IF NOT EXISTS strategy_allocation_opportunity_opportunity_id`
  would add a duplicate of that index on the first refresh.
- strategy_allocation_opportunity.strategy_opportunity_id has no foreign key
  (declared REFERENCES strategy_opportunity ON DELETE CASCADE).
- settings.flex_default_range_days / flex_init_range_days are nullable (declared NOT NULL).
- settings.ib_primary_account_id / stream_primary_account_id: legacy columns,
  NULL in every env and read by no code.
- preference_market_streams_symbol_order's primary key keeps the name of the
  table it was renamed from.
- watchlist_contract_key duplicates watchlist's primary key index.

Every step checks the catalog first, and it only runs ALTER when something
differs. On a database that already matches, it changes nothing and needs no
ownership, so a non-owner role running _ensure_tables is not broken by it.
"""

from __future__ import annotations

from typing import Any

# (table, legacy constraint name, declared name). Renaming a PK constraint renames its index too.
_CONSTRAINT_RENAMES = (
    ("strategy_allocation", "strategy_portfolio_pkey", "strategy_allocation_pkey"),
    (
        "strategy_allocation",
        "strategy_portfolio_gate_safety_strategy_id_fkey",
        "strategy_allocation_gate_safety_strategy_id_fkey",
    ),
    ("strategy_allocation_opportunity", "strategy_portfolio_opportunity_pkey", "strategy_allocation_opportunity_pkey"),
    (
        "preference_market_streams_symbol_order",
        "market_streams_symbol_order_pkey",
        "preference_market_streams_symbol_order_pkey",
    ),
)

_SEQUENCE_RENAME = ("strategy_portfolio_strategy_portfolio_id_seq", "strategy_allocation_strategy_allocation_id_seq")

_INDEX_RENAME = ("strategy_portfolio_opportunity_opportunity_id", "strategy_allocation_opportunity_opportunity_id")

_REDUNDANT_INDEXES = ("watchlist_contract_key",)

_SETTINGS_NOT_NULL = (("flex_default_range_days", 30), ("flex_init_range_days", 360))

_SETTINGS_RETIRED_COLUMNS = ("ib_primary_account_id", "stream_primary_account_id")

_ALLOCATION_OPPORTUNITY_FK = "strategy_allocation_opportunity_strategy_opportunity_id_fkey"


def wave13_statements() -> list[str]:
    """The migration as SQL, in order: what _ensure_tables runs, and what an operator can apply by hand."""
    stmts: list[str] = []
    old_seq, new_seq = _SEQUENCE_RENAME
    stmts.append(
        f"""
        DO $w13$
        BEGIN
          IF to_regclass('public.{old_seq}') IS NOT NULL AND to_regclass('public.{new_seq}') IS NULL THEN
            ALTER SEQUENCE public.{old_seq} RENAME TO {new_seq};
          END IF;
        END $w13$;
        """
    )
    for table, old, new in _CONSTRAINT_RENAMES:
        stmts.append(
            f"""
            DO $w13$
            BEGIN
              IF to_regclass('public.{table}') IS NOT NULL
                 AND EXISTS (SELECT 1 FROM pg_constraint
                             WHERE conrelid = 'public.{table}'::regclass AND conname = '{old}')
                 AND NOT EXISTS (SELECT 1 FROM pg_constraint
                                 WHERE connamespace = 'public'::regnamespace AND conname = '{new}')
                 AND to_regclass('public.{new}') IS NULL THEN
                ALTER TABLE public.{table} RENAME CONSTRAINT {old} TO {new};
              END IF;
            END $w13$;
            """
        )
    old_idx, new_idx = _INDEX_RENAME
    stmts.append(
        f"""
        DO $w13$
        BEGIN
          IF to_regclass('public.{old_idx}') IS NOT NULL THEN
            IF to_regclass('public.{new_idx}') IS NULL THEN
              ALTER INDEX public.{old_idx} RENAME TO {new_idx};
            ELSE
              DROP INDEX public.{old_idx};
            END IF;
          END IF;
        END $w13$;
        """
    )
    for idx in _REDUNDANT_INDEXES:
        stmts.append(
            f"""
            DO $w13$
            BEGIN
              IF to_regclass('public.{idx}') IS NOT NULL THEN
                DROP INDEX public.{idx};
              END IF;
            END $w13$;
            """
        )
    stmts.append(
        f"""
        DO $w13$
        BEGIN
          IF to_regclass('public.strategy_allocation_opportunity') IS NOT NULL
             AND to_regclass('public.strategy_opportunity') IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM pg_constraint
               WHERE conrelid = 'public.strategy_allocation_opportunity'::regclass
                 AND contype = 'f'
                 AND confrelid = 'public.strategy_opportunity'::regclass
             ) THEN
            ALTER TABLE public.strategy_allocation_opportunity
              ADD CONSTRAINT {_ALLOCATION_OPPORTUNITY_FK}
              FOREIGN KEY (strategy_opportunity_id)
              REFERENCES public.strategy_opportunity(strategy_opportunity_id) ON DELETE CASCADE
              NOT VALID;
            BEGIN
              ALTER TABLE public.strategy_allocation_opportunity VALIDATE CONSTRAINT {_ALLOCATION_OPPORTUNITY_FK};
            EXCEPTION WHEN foreign_key_violation THEN
              RAISE WARNING '{_ALLOCATION_OPPORTUNITY_FK} left NOT VALID: rows point at a missing strategy_opportunity';
            END;
          END IF;
        END $w13$;
        """
    )
    for col, default in _SETTINGS_NOT_NULL:
        stmts.append(
            f"""
            DO $w13$
            BEGIN
              IF EXISTS (SELECT 1 FROM information_schema.columns
                         WHERE table_schema = 'public' AND table_name = 'settings'
                           AND column_name = '{col}' AND is_nullable = 'YES') THEN
                UPDATE public.settings SET {col} = {default} WHERE {col} IS NULL;
                ALTER TABLE public.settings ALTER COLUMN {col} SET NOT NULL;
              END IF;
            END $w13$;
            """
        )
    for col in _SETTINGS_RETIRED_COLUMNS:
        stmts.append(
            f"""
            DO $w13$
            BEGIN
              IF EXISTS (SELECT 1 FROM information_schema.columns
                         WHERE table_schema = 'public' AND table_name = 'settings' AND column_name = '{col}') THEN
                ALTER TABLE public.settings DROP COLUMN {col};
              END IF;
            END $w13$;
            """
        )
    return stmts


def migrate_wave13_reconcile_legacy_schema(cur: Any) -> None:
    """Idempotent Wave 13 migration. Run it before the DDL's CREATE INDEX on strategy_allocation_opportunity."""
    for stmt in wave13_statements():
        cur.execute(stmt)
