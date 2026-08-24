# DATABASE.md — bifrost-core schema map

## Golden Source canonical schemas (Wave 6.3)

Use these names in docs, catalogs, and new code. Legacy aliases appear only in migration scripts and historical program YAML.

| Canonical | Legacy alias (historical only) | Owner |
|-----------|-------------------------------|--------|
| `raw_market.*` | `market.*` (Golden Source physical rename) | Market Data Plugin |
| `features.*` | `features_daily.*` / `features_option.*` / `features_signals.*` / `features_forecasts.*` / `features_backtests.*` (Wave 6.6 retired) | Research Feature Store |
| `dw_stock.*` | `analytics.*` | bifrost-research dbt |
| `ops_jobs.*` | `data_ops.*` (**retired Wave 8**) | Market Data + Flex Query Plugins |
| `raw_broker.*` | per-env FDW local name `brokerage.*` | IB / Flex + core FDW |
| `ops_dbt.*` | `analytics_elementary.*` | dbt Elementary |

Retention policy matrix: [GOLDEN_SOURCE_RETENTION.md](../../bifrost-trade-infra/docs/GOLDEN_SOURCE_RETENTION.md) (infra repo).

Authoritative runtime DDL:

| Domain | Module | Database |
|--------|--------|----------|
| Per-env Trade (`strategy_*`, `gate_safety_*`, `preference_*`, `watchlist`, jobs, bridge tables) | [`ddl.py`](../src/bifrost_core/persistence/postgres/ddl.py) `_ensure_tables()` | `bifrost_{dev,stg,prod}` `public.*` |
| Daemon / Account Sync process IPC | [`redis_daemon_state.py`](../src/bifrost_core/persistence/redis_daemon_state.py) — see [DAEMON_IPC_REDIS.md](DAEMON_IPC_REDIS.md) | per-env Redis (`config.redis`) |
| Brokerage Golden Source (IB account / positions / executions) | [`brokerage_ddl.py`](../src/bifrost_core/persistence/postgres/brokerage_ddl.py) | `bifrost_golden_source` `raw_broker.*` |
| Market Data (Polygon) | Market Data Plugin | `bifrost_golden_source` `raw_market.*` / `ops_jobs.*` |
| Flex Query job queue | Flex Query Plugin | `bifrost_golden_source` `ops_jobs.*` (`flex_ops.*` compat views **DEPRECATED** Wave 6.3) |
| Research dbt Elementary | bifrost-research dbt | `bifrost_golden_source` `ops_dbt.*` |

## Per-env vs Golden Source

```
bifrost_golden_source
├── raw_market.*                                   # Polygon Plugin
├── raw_broker.*                                   # IB / Flex brokerage adapter
├── ops_jobs.*                                   # Plugin job queues (market + flex)
├── ops_dbt.*                                    # dbt / Elementary observability
├── dw_stock.*                                   # Research dbt marts (human read)
├── features.*                                   # Research Feature Store (model read)
└── flex_ops.* (DEPRECATED views → ops_jobs; audit only)

bifrost_{dev,stg,prod}
├── public.*          # strategy_*, gate_safety_*, preferences, watchlist, bridge tables (settings)
├── brokerage.*       # postgres_fdw foreign tables → raw_broker + local views
└── market.*          # postgres_fdw foreign tables → raw_market (canonical GS: raw_market.*)
```

Do not create `flex_ops` on Trade env databases. Flex queue + freshness live in Golden Source `ops_jobs` only.

**Compat shims (Golden Source only, not on Trade DBs)**

| Shim | Canonical target | Notes |
|------|------------------|-------|
| `raw_market.stock_financials` (view) | 6 entity financials tables | **Wave 8** compat UNION view; legacy table dropped |
| `flex_ops.*` (views) | `ops_jobs.job_flex_ingest`, `flex_ingest_freshness` | **DEPRECATED** (Wave 6.3); no new consumers |

**Wave 8 — `settings.active_*_id` FK** (core 0.16.0): `ON DELETE SET NULL` to `strategy_structure`, `gate_safety_strategy`, `strategy_allocation`.

**Wave 8 — Flex tokens**: canonical source is K8s Secret (`FLEX_HOST_TOKEN` / `FLEX_SECONDARY_TOKEN`); `settings.ib_flex_*_token` columns **DEPRECATED** (not dropped yet).

Qualified names: [`brokerage_tables.py`](../src/bifrost_core/persistence/postgres/brokerage_tables.py), [`market_tables.py`](../src/bifrost_core/persistence/postgres/market_tables.py).

Writers open `connect_golden_source()`. Readers stay on the per-env connection and JOIN `brokerage.*` via FDW.

Process IPC (heartbeat / run_status / control) is **not** in PostgreSQL. `_ensure_tables()` does not create the retired `daemon_*` / `account_sync_*` IPC tables.

## Gate safety (2 tables)

Safety-boundary config is stored as scalars (no jsonb). Logical grouping (`strategy` / `state` / `intent` / `guard`) exists only in the Python `config['gates']` dict returned by `get_gates_by_id()`.

| Table | Relationship | Purpose |
|-------|--------------|---------|
| `gate_safety_strategy` | 1 row = 1 boundary set | Metadata + strategy/state/intent/guard scalars (~32 columns) |
| `gate_safety_strategy_earnings_dates` | 1:N | Earnings blacklist dates (`holiday_date`) |

Retired (merged into `gate_safety_strategy` in core `0.8.1`): `gate_safety_state`, `gate_safety_intent`, `gate_safety_guard`. `_ensure_tables()` copies remaining child rows then `DROP TABLE`s them.

`settings.active_gate_safety_strategy_id` points at the active set. Opportunity / allocation tables keep FK `*_gate_safety_strategy_id`.

## Brokerage tables

| Golden Source | Legacy public name |
|---------------|--------------------|
| `brokerage.account` | `account` |
| `brokerage.positions` | `account_positions` |
| `brokerage.executions_raw_tws` | `executions_raw_tws` |
| `brokerage.executions_raw_flex` | `executions_raw_flex` |
| `brokerage.executions_raw_journal` | `executions_raw_journal` |
| `brokerage.commissions` | `account_execution_commissions` |
| `brokerage.transactions` | `account_transactions` |
| `brokerage.open_orders` | `daemon_open_orders` |
| `brokerage.contract_quote_live` | `contract_quote_live` |
| `brokerage.settings_flex` | `settings_ib_flex` |
| views `brokerage.executions*` | `account_executions*` |

Bridge tables remain per-env (FK to `strategy_instance`):

- `account_execution_instance_allocation`
- `account_execution_option_stock_link`

`_ensure_tables()` does **not** recreate migrated brokerage objects in `public`.
`option_trades` is P7-retired (Market Data Plugin) and is also not created.

## Market FDW tables (core 0.8.3)

| Per-env FDW | Golden Source | Purpose |
|-------------|---------------|---------|
| `market.ticker` | `raw_market.ticker` | Full ticker catalog (FDW foreign table) |
| `market.v_us_equity_universe` | `raw_market.v_us_equity_universe` | Local VIEW: active US CS equities |
| `public.v_us_equity_universe` | — | **Stable Trade read contract** over `market.v_us_equity_universe` (adds synthetic `tickers_id`); not scheduled for removal |

Setup: `setup_fdw_market_tables()` in [`brokerage_ddl.py`](../src/bifrost_core/persistence/postgres/brokerage_ddl.py). Requires `golden_source_server` to exist (created by `setup_fdw_foreign_tables`).

Retired (core 0.8.3): `public.us_equity_universe` (physical table), `public.sepa_symbol_price_readiness` (physical table), `public.v_sepa_us_equity_universe` (view), `public.v_sepa_symbol_price_readiness` (view), `universe_sync.py` (Plugin API sync module). Universe data now comes directly from Golden Source via FDW. Price readiness summary is computed at query time from Plugin API `/readiness/bar-aggregate`.

Retired (core **0.10.10** / Market Data Plugin **0.7.9**): `public.preference_data_gap_ack` (and legacy `preference_sepa_gap_ack`) — source-void acknowledgments now live in Golden Source `ops_jobs.data_source_void` via Plugin `/market/readiness/source-void`. Trade `/research/data/readiness/*` is a thin HTTP passthrough for readiness summary / gap-ack / backfill enqueue.

Retired (core **0.15.0** / Wave 6): `public.ops_audit_log` — actuation audit routed to platform-api `POST /api/v1/audit/append`; table dropped idempotently on `_ensure_tables()`.

## Commands

```bash
make db-init                 # per-env DDL + brokerage schema + FDW (if golden_source configured)
make db-init-brokerage       # Golden Source brokerage DDL only
make db-init-brokerage-fdw   # + FDW into current per-env DB (needs superuser)
```

See also [BROKERAGE_GOLDEN_SOURCE.md](BROKERAGE_GOLDEN_SOURCE.md), [DAEMON_IPC_REDIS.md](DAEMON_IPC_REDIS.md), and [GOLDEN_SOURCE_RETENTION.md](../../bifrost-trade-infra/docs/GOLDEN_SOURCE_RETENTION.md).
