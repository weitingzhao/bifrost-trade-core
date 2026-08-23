"""Drop retired SEPA Phase4 PG job queue (replaced by analytics.sepa_screener_wide)."""

-- Run on bifrost_dev / bifrost_stg / bifrost_prod after SEPA_USE_ANALYTICS cutover.
DROP TABLE IF EXISTS public.job_sepa_phase4;
