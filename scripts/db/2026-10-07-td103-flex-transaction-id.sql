-- TD-103: backfill flex_transaction_id, then a partial unique index.
-- Golden Source only (bifrost_golden_source.raw_broker.transactions).
--
-- Dry-run on the replica, 2026-10-07, read-only:
--   rows=171  id_null=171  raw transactionID present=171  would_fill=171
--   still null after backfill=0  duplicate (account_id, id) groups=0
-- Re-run the SELECT below before the UPDATE. If would_fill changed or the
-- duplicate query is not 0, stop.
--
-- Order, and why the writer change is not in this branch:
--   1. Deploy the flex parser that reads the transactionID attribute
--      (bifrost-platform-plugin-flex-query, cursor/d2-flex) so new rows arrive
--      with an id.
--   2. Run the UPDATE below.
--   3. Point upsert_account_transactions at this partial unique index when
--      flex_transaction_id is present, and keep the old
--      (account_id, ts, amount, type, report_date) target only for id-less rows.
--      That edit is accounts.py. LANE-T already has that file dirty on
--      cursor/t-core (TD-91), so this lane did not touch it.
--   4. Only then run the CREATE UNIQUE INDEX. Until step 3, an INSERT that
--      conflicts on (account_id, flex_transaction_id) errors instead of updating,
--      because ON CONFLICT still names the old key.
--
-- The UPDATE is a write. This file is prepared, not executed.
-- The CREATE UNIQUE INDEX CONCURRENTLY must be its own statement, not inside
-- a transaction (no psql -1, and not in the same script invocation as the
-- UPDATE if you pass -1). Run them as two psql calls.

-- --- dry-run (read-only; safe to run as-is) ---
SELECT count(*) AS rows,
       count(*) FILTER (WHERE flex_transaction_id IS NULL) AS id_null,
       count(*) FILTER (
           WHERE flex_transaction_id IS NULL
             AND coalesce(raw_extra->>'transactionID', '') <> ''
       ) AS would_fill,
       count(*) FILTER (
           WHERE flex_transaction_id IS NULL
             AND coalesce(raw_extra->>'transactionID', '') = ''
       ) AS still_null
FROM raw_broker.transactions;

SELECT count(*) AS duplicate_id_groups
FROM (
    SELECT 1
    FROM raw_broker.transactions
    WHERE coalesce(flex_transaction_id, raw_extra->>'transactionID', '') <> ''
    GROUP BY account_id, coalesce(flex_transaction_id, raw_extra->>'transactionID')
    HAVING count(*) > 1
) d;

-- --- backfill (Owner; do not combine with CONCURRENTLY under psql -1) ---
-- UPDATE raw_broker.transactions
-- SET flex_transaction_id = raw_extra->>'transactionID'
-- WHERE flex_transaction_id IS NULL
--   AND coalesce(raw_extra->>'transactionID', '') <> '';

-- --- partial unique index (Owner, after the writer uses it; own psql, no -1) ---
-- CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS transactions_account_flex_tx_uidx
--     ON raw_broker.transactions (account_id, flex_transaction_id)
--     WHERE flex_transaction_id IS NOT NULL;
