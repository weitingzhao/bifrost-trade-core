"""Drop retired Trade Celery job_ticker_reference_state table (ticker reference sync cursors)."""

-- Run on bifrost_dev / bifrost_stg / bifrost_prod after Market Data Plugin ticker_sync cutover.
DROP TABLE IF EXISTS public.job_ticker_reference_state;
