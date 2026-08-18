# Brokerage Golden Source

IB / brokerage account data lives in a shared schema on `bifrost_golden_source`,
symmetric to Market Data (`market.*`).

## Layout

```
bifrost_golden_source
├── market.* / market_analytics.* / data_ops.*   # Polygon (Plugin)
└── brokerage.*                                  # Brokerage / IB adapter
    ├── account, positions
    ├── executions_raw_{tws,flex,journal}
    ├── commissions, transactions
    ├── open_orders, contract_quote_live
    ├── settings_flex
    └── views: executions, executions_final, executions_fly

bifrost_{dev,stg,prod}
├── public.*          # daemon_*, strategy_*, preferences, bridge tables
└── brokerage.*       # postgres_fdw foreign tables + local views (read path)
```

## Connection

Config key `golden_source` (see `config.yaml.example`). Writers open a direct
connection via `_get_golden_source_conn_params()` / `connect_golden_source()`.
Readers stay on the per-env connection and query `brokerage.*` through FDW.

## Commands

```bash
make db-init-brokerage          # DDL on golden_source
make db-init-brokerage-fdw      # + FDW into current per-env DB (needs superuser)
.venv/bin/python scripts/db/migrate_brokerage_data.py --source-db bifrost_prod
```

## Bridge tables (per-env)

- `account_execution_instance_allocation` — FK to `strategy_instance`; exec id integrity at app layer
- `account_execution_option_stock_link` — same pattern

## Per-env DDL

`ddl.py` `_ensure_tables()` skips migrated brokerage tables/views (core `0.6.1`).
K8s `db_refresh_schema.py` also runs `ensure_brokerage_schema()` + FDW when
`golden_source` is present in config.

## Cleanup

Legacy `public.account*` / `executions_raw_*` / `daemon_open_orders` /
`contract_quote_live` / `settings_ib_flex` were renamed `*_legacy_bak`
and are retained for a 30-day observation window before DROP.
