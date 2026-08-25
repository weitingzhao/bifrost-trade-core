"""Drop retired Trade Celery job_bars_backfill table (stocks_ib bars backfill)."""

-- Run on bifrost_dev / bifrost_stg / bifrost_prod after API cutover to Plugin enqueue.
DROP TABLE IF EXISTS public.job_bars_backfill;
