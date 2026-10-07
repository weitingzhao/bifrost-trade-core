-- TD-103 partial unique index. Own psql session. No -1, no surrounding BEGIN.
-- Run only after the backfill and after the writer uses this index as the
-- conflict target for rows that have flex_transaction_id. Until then a second
-- insert of the same id errors instead of updating.
--
-- Rollback: scripts/db/2026-10-07-td103-flex-transaction-id-index-rollback.sql

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS transactions_account_flex_tx_uidx
    ON raw_broker.transactions (account_id, flex_transaction_id)
    WHERE flex_transaction_id IS NOT NULL;
