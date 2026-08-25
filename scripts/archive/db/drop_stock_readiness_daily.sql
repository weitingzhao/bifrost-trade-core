"""Drop retired Trade SEPA stock_readiness_daily snapshot table."""

-- Run on bifrost_dev / bifrost_stg / bifrost_prod after analytics mart cutover.
DROP TABLE IF EXISTS public.stock_readiness_daily CASCADE;
