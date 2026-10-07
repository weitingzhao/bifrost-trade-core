-- TD-103 index rollback. Own psql session. No -1.
-- Does not clear flex_transaction_id. The column values stay.

DROP INDEX CONCURRENTLY IF EXISTS raw_broker.transactions_account_flex_tx_uidx;
