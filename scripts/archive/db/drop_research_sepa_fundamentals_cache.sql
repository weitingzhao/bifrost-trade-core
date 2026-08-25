-- Drop retired Trade SEPA fundamentals cache table and dependent view.
-- Safe to run on bifrost_dev / bifrost_stg / bifrost_prod when analytics marts are active.

DROP VIEW IF EXISTS public.v_sepa_symbol_fund_cache_readiness CASCADE;
DROP TABLE IF EXISTS public.research_sepa_fundamentals_cache CASCADE;
