# Brokerage Golden Source

IB / brokerage account data lives in a shared schema on `bifrost_golden_source`,
symmetric to Market Data (`raw_market.*`).

## Layout

```
bifrost_golden_source
├── raw_market.* / features_daily.* / ops_jobs.*   # Polygon (Market Data Plugin)
└── raw_broker.*                                   # Brokerage / IB adapter (canonical)
    ├── account, positions
    ├── executions_raw_{tws,flex,journal}
    ├── commissions, transactions
    ├── open_orders, contract_quote_live
    ├── settings_flex
    └── views: executions, executions_final, executions_fly

bifrost_{dev,stg,prod}
├── public.*          # strategy_*, preferences, execution bridge tables
│                     # (daemon IPC is Redis — see docs/DAEMON_IPC_REDIS.md)
└── brokerage.*       # postgres_fdw foreign tables + local views → raw_broker.*
```

**Legacy names (historical only):** Golden Source `market.*` → `raw_market.*`; `market_analytics.*` → `features_daily.*`; `data_ops.*` → `ops_jobs.*` (view shim may remain for platform-api probe).

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

## Cleanup (completed 2026-08-18)

Legacy `public.account*` / `executions_raw_*` / `daemon_open_orders` /
`contract_quote_live` / `settings_ib_flex` were renamed `*_legacy_bak`
during migration. All cleanup is now complete:

- **Empty public shells** (0-row recreates from old `_ensure_tables`): dropped
  in DEV/STG/PROD on 2026-08-18 after workers upgraded to core ≥ 0.7.1.
- **`*_legacy_bak` tables/views** (10 tables + 3 views per env): dropped in
  DEV/STG/PROD on 2026-08-18 after confirming Golden Source is a strict
  superset (+4 flex / +4 commissions / +8 transactions of new production data)
  and zero code references remain.

No brokerage-related objects remain in `public` schema. All reads and writes
go through `brokerage.*` (Golden Source physical / FDW foreign tables).

Daemon heartbeat / control / run_status tables were also dropped from `public`
(core `0.8.0`); see `docs/DAEMON_IPC_REDIS.md`.
