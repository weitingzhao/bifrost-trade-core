-- TD-103 apply. Two separate psql invocations. Neither uses -1 across both
-- statements: CREATE INDEX CONCURRENTLY cannot run inside a transaction block.
--
-- Call 1 (the backfill). Re-run the dry-run SELECT in
-- 2026-10-07-td103-flex-transaction-id.sql first; stop if duplicate_id_groups <> 0.
UPDATE raw_broker.transactions
SET flex_transaction_id = raw_extra->>'transactionID'
WHERE flex_transaction_id IS NULL
  AND coalesce(raw_extra->>'transactionID', '') <> '';

-- Call 2, only after upsert_account_transactions conflicts on this index when
-- flex_transaction_id is present (see the header of the dry-run file). Own session:
-- CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS transactions_account_flex_tx_uidx
--     ON raw_broker.transactions (account_id, flex_transaction_id)
--     WHERE flex_transaction_id IS NOT NULL;
