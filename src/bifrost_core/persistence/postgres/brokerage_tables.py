"""Qualified table/view names for brokerage Golden Source schema.

Physical tables live in ``bifrost_golden_source.raw_broker.*`` (imported into per-env
``brokerage.*`` via postgres_fdw). Per-env DBs expose ``brokerage.*`` foreign tables
so readers can JOIN brokerage data with public strategy/preference tables on one connection.
"""

from __future__ import annotations

SCHEMA = "brokerage"
GOLDEN_SCHEMA = "raw_broker"

# Per-env FDW local schema (brokerage.* foreign tables + views)
ACCOUNT = f"{SCHEMA}.account"
POSITIONS = f"{SCHEMA}.positions"
EXECUTIONS_RAW_TWS = f"{SCHEMA}.executions_raw_tws"
EXECUTIONS_RAW_FLEX = f"{SCHEMA}.executions_raw_flex"
EXECUTIONS_RAW_JOURNAL = f"{SCHEMA}.executions_raw_journal"
COMMISSIONS = f"{SCHEMA}.commissions"
TRANSACTIONS = f"{SCHEMA}.transactions"
OPEN_ORDERS = f"{SCHEMA}.open_orders"
CONTRACT_QUOTE_LIVE = f"{SCHEMA}.contract_quote_live"
SETTINGS_FLEX = f"{SCHEMA}.settings_flex"

# Golden Source physical writes (bifrost_golden_source.raw_broker.*)
GOLDEN_ACCOUNT = f"{GOLDEN_SCHEMA}.account"
GOLDEN_POSITIONS = f"{GOLDEN_SCHEMA}.positions"
GOLDEN_EXECUTIONS_RAW_TWS = f"{GOLDEN_SCHEMA}.executions_raw_tws"
GOLDEN_EXECUTIONS_RAW_FLEX = f"{GOLDEN_SCHEMA}.executions_raw_flex"
GOLDEN_EXECUTIONS_RAW_JOURNAL = f"{GOLDEN_SCHEMA}.executions_raw_journal"
GOLDEN_COMMISSIONS = f"{GOLDEN_SCHEMA}.commissions"
GOLDEN_TRANSACTIONS = f"{GOLDEN_SCHEMA}.transactions"
GOLDEN_OPEN_ORDERS = f"{GOLDEN_SCHEMA}.open_orders"
GOLDEN_CONTRACT_QUOTE_LIVE = f"{GOLDEN_SCHEMA}.contract_quote_live"
GOLDEN_SETTINGS_FLEX = f"{GOLDEN_SCHEMA}.settings_flex"

# Views (Flex-authoritative merge + performance / on-the-fly subsets)
EXECUTIONS = f"{SCHEMA}.executions"
EXECUTIONS_FINAL = f"{SCHEMA}.executions_final"
EXECUTIONS_FLY = f"{SCHEMA}.executions_fly"

# Per-env trade attribution (TD-09, core 0.37.0; renamed in naming R3, core 0.45.0). One
# table in each env's public schema, keyed by the fill (account_id, exec_id) -- the TWS row
# and its Flex twin share it -- with a real FK to that env's trade. A NULL split_quantity
# is the whole fill; split rows carry their share. Golden Source's strategy_* columns on
# the raw tables are no longer written or read.
TRADE_EXECUTION = "trade_execution"
# Fill splits as readers join them: one row per raw representation (Flex id, TWS -id,
# journal -(1e9+id)) of each split fill, (account_id, account_executions_id, trade_id,
# quantity, exec_id). A local view over the FDW tables, rebuilt with them
# (``brokerage_ddl._create_brokerage_views``).
TRADE_FILL_SPLITS = f"{SCHEMA}.trade_fill_splits"
# Naming R4 (core 0.47.0) removed the one-version names of R3: the constants
# INSTANCE_EXECUTION / INSTANCE_ALLOCATION / COMPAT_INSTANCE_ALLOCATIONS, the env view
# brokerage.instance_allocations (no longer made; RETIRED_ENV_VIEWS drops it wherever the env
# views are rebuilt) and the compatibility views public.strategy_instance /
# public.strategy_instance_execution. The Owner's R4 db-step (drop_trade_compat) drops all
# three and rebuilds the env views in dev / stg / prod, whose db-init skips its FDW step.
# TWS raw rows with the synthetic account_executions_id and this env's attribution
# (the ``tws_raw`` scope and the position attribution's no-Flex branch).
EXECUTIONS_TWS = f"{SCHEMA}.executions_tws"
# Before TD-09 the splits were account_execution_instance_allocation (per env, keyed by
# account_executions_id). Frozen since core 0.37.0; core 0.47.0 no longer creates it and the
# Owner's R4 db-step drops it after a CSV export (D7-A).
OPTION_STOCK_LINK = "account_execution_option_stock_link"

# Legacy public names → brokerage qualified (for migration scripts / docs)
LEGACY_TO_BROKERAGE: dict[str, str] = {
    "account": ACCOUNT,
    "account_positions": POSITIONS,
    "executions_raw_tws": EXECUTIONS_RAW_TWS,
    "executions_raw_flex": EXECUTIONS_RAW_FLEX,
    "executions_raw_journal": EXECUTIONS_RAW_JOURNAL,
    "account_execution_commissions": COMMISSIONS,
    "account_transactions": TRANSACTIONS,
    "daemon_open_orders": OPEN_ORDERS,
    "contract_quote_live": CONTRACT_QUOTE_LIVE,
    "settings_ib_flex": SETTINGS_FLEX,
    "account_executions": EXECUTIONS,
    "account_executions_final": EXECUTIONS_FINAL,
    "account_executions_fly": EXECUTIONS_FLY,
}

BROKERAGE_PHYSICAL_TABLES: tuple[str, ...] = (
    "account",
    "positions",
    "executions_raw_tws",
    "executions_raw_flex",
    "executions_raw_journal",
    "commissions",
    "transactions",
    "open_orders",
    "contract_quote_live",
    "settings_flex",
)

BROKERAGE_VIEWS: tuple[str, ...] = (
    "executions",
    "executions_final",
    "executions_fly",
)

# Per-env only: they join this env's attribution table (not on Golden Source).
BROKERAGE_ENV_VIEWS: tuple[str, ...] = (
    "executions_tws",
    "trade_fill_splits",
)
