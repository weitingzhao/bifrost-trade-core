-- TD-148: widen strategy_plan.source_kind. Widening only.
--
-- Constraint name read 2026-10-07 from pg_constraint on the replica, contype 'c',
-- conrelid public.strategy_plan, definition containing source_kind:
--   bifrost_dev  strategy_plan_source_kind_check
--   bifrost_stg  strategy_plan_source_kind_check
--   bifrost_prod strategy_plan_source_kind_check
-- Same name in all three, so this file is the statement for each environment.
-- Run it once per database (bifrost_dev, bifrost_stg, bifrost_prod), not on
-- bifrost_golden_source. ACCESS EXCLUSIVE for the constraint swap; the table
-- is tiny (DEV 3 plans, STG 0, PROD 0, read 2026-10-07). Existing rows use
-- only the five old values, so they pass the new check.
--
-- Do not run while a session is inserting a source_kind outside the new list.
-- Rollback: scripts/db/2026-10-07-td148-strategy-plan-source-kind-rollback.sql
-- (refuses if any row already uses lens or backtest_run).

ALTER TABLE strategy_plan DROP CONSTRAINT strategy_plan_source_kind_check;

ALTER TABLE strategy_plan
    ADD CONSTRAINT strategy_plan_source_kind_check
    CHECK (source_kind IN (
        'manual', 'symbol', 'hypothesis', 'inbox_draft', 'roll', 'lens', 'backtest_run'
    ));
