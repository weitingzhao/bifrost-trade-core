-- TD-246 dry run: existing position_snapshot_daily option rows whose stored mark is under the
-- option's intrinsic value at the stored underlying close. READ-ONLY: it lists, it changes nothing.
--
-- Core 0.56.0 stops enrich from writing such a mark (snapshot.daily.option_eod_mark); rows written
-- before it keep theirs. Restating them is an Owner decision (not approved with TD-246). If approved,
-- the new value per row is option_eod_mark(mark, iv=iv, strike, right, expiry, underlying_close,
-- session=snapshot_date) -- Black-Scholes in Python, which this SQL does not reproduce.
--
-- Run per env database (bifrost_dev / bifrost_stg / bifrost_prod), e.g.
--   psql -X -d bifrost_dev -v ON_ERROR_STOP=1 -f scripts/db/td246_marks_under_intrinsic_dryrun.sql
-- The tolerance (0.01) is snapshot.daily.INTRINSIC_TOLERANCE, the reader's mark_below_intrinsic rule.

BEGIN READ ONLY;

WITH opt AS (
    SELECT position_snapshot_daily_id, snapshot_date, account_id, contract_key, trade_id,
           symbol, expiry, strike, option_right, trade_qty, mark, mark_source, underlying_close, iv,
           CASE WHEN upper(left(option_right, 1)) = 'C' THEN greatest(underlying_close - strike, 0)
                WHEN upper(left(option_right, 1)) = 'P' THEN greatest(strike - underlying_close, 0)
           END AS intrinsic
    FROM position_snapshot_daily
    WHERE upper(coalesce(sec_type, '')) = 'OPT'
      AND mark IS NOT NULL AND underlying_close IS NOT NULL AND strike IS NOT NULL
)
SELECT position_snapshot_daily_id, snapshot_date, account_id, contract_key, trade_id,
       mark_source, mark, round(intrinsic::numeric, 4) AS intrinsic,
       round((intrinsic - mark)::numeric, 4) AS under_by, underlying_close, iv,
       expiry - snapshot_date AS days_to_expiry,
       -- what enrich 0.56.0 would label it (the value needs the Python model)
       CASE WHEN mark_source IS DISTINCT FROM 'vendor_eod' THEN 'not enrich''s (' || coalesce(mark_source, 'null') || '): leave'
            WHEN iv > 0 AND expiry > snapshot_date THEN 'vendor_iv_model (or intrinsic_floor if the model is under intrinsic)'
            ELSE 'intrinsic_floor'
       END AS would_be
FROM opt
WHERE mark < intrinsic - 0.01
ORDER BY snapshot_date, contract_key, trade_id NULLS LAST;

-- Per session: how many option rows, how many under intrinsic.
SELECT snapshot_date, count(*) AS option_rows,
       count(*) FILTER (WHERE mark < intrinsic - 0.01) AS under_intrinsic
FROM (
    SELECT snapshot_date, mark,
           CASE WHEN upper(left(option_right, 1)) = 'C' THEN greatest(underlying_close - strike, 0)
                WHEN upper(left(option_right, 1)) = 'P' THEN greatest(strike - underlying_close, 0)
           END AS intrinsic
    FROM position_snapshot_daily
    WHERE upper(coalesce(sec_type, '')) = 'OPT'
) t
GROUP BY snapshot_date
ORDER BY snapshot_date;

ROLLBACK;
