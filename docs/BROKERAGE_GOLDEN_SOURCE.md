# Brokerage Golden Source

IB / brokerage account data lives in a shared schema on `bifrost_golden_source`,
symmetric to Market Data (`raw_market.*`).

## Layout

```
bifrost_golden_source
├── raw_market.* / ops_jobs.*                      # Polygon (Market Data Plugin)
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

**Legacy names (historical only):** Golden Source `market.*` → `raw_market.*`; `market_analytics.*` → `features_daily.*` → `features.*` (Research Feature Store, Wave 6.6); `data_ops.*` → `ops_jobs.*`. Canonical names: [DATABASE.md](DATABASE.md#golden-source-canonical-schemas-wave-63).

## Connection

Config key `golden_source` (see `config.yaml.example`). Writers open a direct
connection with `psycopg2.connect(**_get_golden_source_conn_params(config))` and write
`raw_broker.*` (the `GOLDEN_*` names in `brokerage_tables.py`).
Readers stay on the per-env connection and query `brokerage.*` through FDW.

## Commands

```bash
make db-init-brokerage          # DDL on golden_source
make db-init-brokerage-fdw      # + FDW into current per-env DB (needs superuser)
.venv/bin/python scripts/db/migrate_brokerage_data.py --source-db bifrost_prod
```

## Columns that are not vendor fields

`raw_broker` is a vendor-shaped layer: column names follow IB / Flex, and the Rev .111 plan C rename left it
unchanged. The database-design skill exempts it from the `<table>_id` / FK-name rules. These columns are
Bifrost's own, not IB's — DDL in [`brokerage_ddl.py`](../src/bifrost_core/persistence/postgres/brokerage_ddl.py).

### Keys

| Table | Key | Notes |
|-------|-----|-------|
| `executions_raw_tws` / `_flex` / `_journal` | `executions_raw_{tws,flex,journal}_id` bigserial | Follows `<table>_id`. Dedupe: partial UNIQUE on `exec_id` (non-empty); Flex also on `(account_id, trade_id)` |
| `transactions` | `account_transactions_id` bigserial | Legacy name from `public.account_transactions`. Dedupe: UNIQUE `(account_id, ts, amount, type, report_date)` (Wave 3) |
| `open_orders` | `id` bigserial | Generic `id` on a multi-row table (legacy `daemon_open_orders`). The writer replaces the whole table each time (`TRUNCATE` + `INSERT`), so the id carries no identity — `order_id` / `perm_id` are IB's |
| `settings_flex` | `id` serial | Generic `id` (legacy `settings_ib_flex`); one row per Flex query (`sort_order`, `query_label`, `purpose`, `query_host_id`, `query_secondary_id`) |
| `account` | `account_id` | IB account code |
| `positions` | `(account_id, contract_key)` | |
| `commissions` | `exec_id` | IB execution id; `yield_` / `yield_redemption_date` are IB's `CommissionReport.yield` / `yieldRedemptionDate` (`yield` is a Python keyword) |
| `contract_quote_live` | `contract_key` | |

### Unified execution id

The views `executions`, `executions_final`, `executions_fly` expose one `account_executions_id` across the three
raw tables — Flex `executions_raw_flex_id` (> 0), TWS `-executions_raw_tws_id`, journal
`-(1000000000 + executions_raw_journal_id)`. `executions` drops a TWS row whose `exec_id` Flex also has;
`executions_final` is Flex + journal only; `executions_fly` is TWS rows (not `BAG`) with no Flex/journal
counterpart. The per-env bridge tables key on this id
([DATABASE.md](DATABASE.md#brokerage-tables)); `_raw_table_pk_for_account_executions_id()` in
[`accounts.py`](../src/bifrost_core/portfolio/reader/accounts.py) maps it back to a raw row.

### Strategy attribution and legacy columns

All three `executions_raw_*` tables carry:

| Column | Type | Meaning |
|--------|------|---------|
| `strategy_opportunity_id` | bigint | **Frozen since core 0.37.0 (TD-09).** Was the whole-execution attribution to a per-env `strategy_opportunity`. No longer written or read by Trade; the per-env views no longer expose it (the Golden Source `raw_broker.executions*` views still do) |
| `strategy_instance_id` | bigint | **Frozen since core 0.37.0 (TD-09).** Was the whole-execution attribution to a per-env `strategy_instance` |
| `legacy_account_executions_id` | bigint | Historical map: the row's id in the single pre-split `account_executions` table. Rows from before the split keep it; no code writes it (new rows get NULL by default since core 0.32.0) or reads it, and the views do not expose it |

**Why they were retired.** These ids point at per-env tables, so the database could not check them, and DEV,
STG and PROD all read the same `raw_broker` rows: an id written from one environment named a row in that
environment's `strategy_instance` and read as a different (or missing) instance in the other two. On
2026-10-03 five instance ids (158–162) named different trades in DEV and PROD, and 40 instances still carried
an opportunity id their environment had since changed.

**Since core 0.37.0** each environment keeps its own attribution in `public.trade_execution` (named
`strategy_instance_execution` until naming R3, core 0.45.0), keyed by the fill (`account_id`, `exec_id`) — a TWS
row and its Flex twin share it — with a composite FK to that environment's `trade (trade_id, account_id)`; see
[DATABASE.md](DATABASE.md#trade_execution-core-0370-as-strategy_instance_execution-renamed-in-0450). The columns here
were kept, unwritten, as the rollback path; clearing or dropping them is a separate Owner decision (D10′-A). The
one-off move was `td09_attribution` (`scripts/db/td09_migrate_attribution.py`, retired in core 0.45.0 after it ran
on dev / stg / prod on 2026-10-03; it is in git history up to tag v0.44.0).

### Env view columns (per-env `brokerage.executions*`, naming R3, core 0.45.0)

The Golden Source views (`raw_broker.executions`, `_final`, `_fly`) keep the vendor-shaped names (§7 exemption of
the database-design standard). Each env's `brokerage.executions` / `executions_final` / `executions_fly` /
`executions_tws` are built over the FDW tables (`brokerage_views._create_brokerage_views(env=True)`) with the same
columns in the same order, except:

| Golden Source column | Env view column(s) | Meaning in the env view |
|----------------------|--------------------|-------------------------|
| `trade_id` | `ib_trade_id` | IB Flex TradeID (vendor); renamed so the env has one `trade_id` (TD-13) |
| `related_trade_id` | `ib_related_trade_id` | IB Flex RelatedTradeID (vendor) |
| `strategy_opportunity_id` | `strategy_opportunity_id` | The env trade's opportunity (not the frozen raw column) |
| `strategy_instance_id` | `trade_id`, then `strategy_instance_id` | `trade_id`: the env's Trade from the whole-fill row of `public.trade_execution`; `strategy_instance_id` (= `trade_id`) only for one version, for pods on core < 0.45.0 — R4 drops it |

Env-only views: `brokerage.trade_fill_splits` (`account_id`, `account_executions_id`, `trade_id`, `quantity`,
`exec_id` — one row per raw representation of each split fill) and, one version, `brokerage.instance_allocations`
over it with core 0.44.0's columns (`strategy_instance_id`, `allocated_quantity`). Before R3 (core 0.37.0–0.44.0) the
env views had Golden Source's columns exactly, with `strategy_instance_id` meaning this env's attribution.

## Bridge tables (per-env)

- `trade_execution` (core 0.37.0 as `strategy_instance_execution`; renamed in 0.45.0) — the trade attribution of
  a fill, whole or split (`split_quantity`); keyed by (`account_id`, `exec_id`), composite FK to `trade`
- `account_execution_instance_allocation` — **frozen since core 0.37.0**: the splits before TD-09, keyed by the
  unified execution id; no longer written or read (the TD-09 migration read it); dropped in naming R4
- `account_execution_option_stock_link` — option execution ↔ stock fill(s) of its exercise / assignment;
  no FK at all (both ends are unified execution ids)

Columns: [DATABASE.md appendix](DATABASE.md#appendix--public-columns-bifrost_dev-2026-10-01).

## Per-env DDL

`ddl.py` `_ensure_tables()` skips migrated brokerage tables/views (core `0.6.1`).
The K8s db-init Job (trade-api image, `scripts/run_db_refresh_schema.py`) also runs
`ensure_brokerage_schema()` + both FDW setups when `golden_source` is present in config.

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
