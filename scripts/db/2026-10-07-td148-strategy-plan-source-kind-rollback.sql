-- TD-148 rollback. Refuses while any row uses a value the old check rejects.
-- Run in one transaction so a refusal leaves the widened check in place.

BEGIN;

DO $$
DECLARE
    n integer;
BEGIN
    SELECT count(*) INTO n
    FROM strategy_plan
    WHERE source_kind IN ('lens', 'backtest_run');
    IF n <> 0 THEN
        RAISE EXCEPTION 'TD-148 rollback refused: % row(s) use lens or backtest_run', n;
    END IF;
END $$;

ALTER TABLE strategy_plan DROP CONSTRAINT strategy_plan_source_kind_check;

ALTER TABLE strategy_plan
    ADD CONSTRAINT strategy_plan_source_kind_check
    CHECK (source_kind IN ('manual', 'symbol', 'hypothesis', 'inbox_draft', 'roll'));

COMMIT;
