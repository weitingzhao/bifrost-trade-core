-- Golden Source: merge duplicate brokerage.* into raw_broker.*, then drop legacy compat schemas.
-- Run as CNPG postgres superuser against bifrost_golden_source.
-- Idempotent: safe to re-run (merge skips existing keys).

BEGIN;

-- ── 1. Merge stale brokerage physical tables into raw_broker (if brokerage schema exists) ──

DO $merge$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'brokerage') THEN
    RAISE NOTICE 'brokerage schema absent — skip merge';
    RETURN;
  END IF;

  INSERT INTO raw_broker.account
  SELECT * FROM brokerage.account b
  WHERE NOT EXISTS (SELECT 1 FROM raw_broker.account r WHERE r.account_id = b.account_id);

  INSERT INTO raw_broker.positions
  SELECT * FROM brokerage.positions b
  WHERE NOT EXISTS (
    SELECT 1 FROM raw_broker.positions r
    WHERE r.account_id = b.account_id AND r.contract_key = b.contract_key
  );

  INSERT INTO raw_broker.contract_quote_live
  SELECT * FROM brokerage.contract_quote_live b
  WHERE NOT EXISTS (SELECT 1 FROM raw_broker.contract_quote_live r WHERE r.contract_key = b.contract_key);

  INSERT INTO raw_broker.commissions
  SELECT * FROM brokerage.commissions b
  WHERE NOT EXISTS (SELECT 1 FROM raw_broker.commissions r WHERE r.exec_id = b.exec_id);

  INSERT INTO raw_broker.transactions
  SELECT * FROM brokerage.transactions b
  WHERE NOT EXISTS (
    SELECT 1 FROM raw_broker.transactions r
    WHERE r.account_id = b.account_id
      AND r.ts = b.ts
      AND r.amount = b.amount
      AND r.type = b.type
  );

  INSERT INTO raw_broker.open_orders
  SELECT * FROM brokerage.open_orders b
  WHERE NOT EXISTS (SELECT 1 FROM raw_broker.open_orders r WHERE r.order_id = b.order_id);

  INSERT INTO raw_broker.settings_flex
  SELECT * FROM brokerage.settings_flex b
  WHERE NOT EXISTS (SELECT 1 FROM raw_broker.settings_flex r WHERE r.id = b.id);

  INSERT INTO raw_broker.executions_raw_tws
  SELECT * FROM brokerage.executions_raw_tws b
  WHERE b.exec_id IS NOT NULL AND b.exec_id != ''
    AND NOT EXISTS (SELECT 1 FROM raw_broker.executions_raw_tws r WHERE r.exec_id = b.exec_id);

  INSERT INTO raw_broker.executions_raw_flex
  SELECT * FROM brokerage.executions_raw_flex b
  WHERE b.exec_id IS NOT NULL AND b.exec_id != ''
    AND NOT EXISTS (SELECT 1 FROM raw_broker.executions_raw_flex r WHERE r.exec_id = b.exec_id);

  INSERT INTO raw_broker.executions_raw_journal
  SELECT * FROM brokerage.executions_raw_journal b
  WHERE b.exec_id IS NOT NULL AND b.exec_id != ''
    AND NOT EXISTS (SELECT 1 FROM raw_broker.executions_raw_journal r WHERE r.exec_id = b.exec_id);

  RAISE NOTICE 'brokerage → raw_broker merge complete';
END
$merge$;

-- Drop duplicate Golden Source brokerage schema (per-env FDW uses local brokerage over raw_broker).
DROP SCHEMA IF EXISTS brokerage CASCADE;

-- ── 2. Drop legacy compat view schemas (Wave 5) ──
DROP SCHEMA IF EXISTS market CASCADE;
DROP SCHEMA IF EXISTS analytics CASCADE;
DROP SCHEMA IF EXISTS market_analytics CASCADE;
DROP SCHEMA IF EXISTS research CASCADE;
DROP SCHEMA IF EXISTS data_ops CASCADE;
DROP SCHEMA IF EXISTS flex_ops CASCADE;
DROP SCHEMA IF EXISTS analytics_elementary CASCADE;

COMMIT;
