# DATABASE.md — bifrost-core schema map

Authoritative runtime DDL:

| Domain | Module | Database |
|--------|--------|----------|
| Per-env Trade (`strategy_*`, `gate_safety_*`, `preference_*`, `watchlist`, jobs, bridge tables) | [`ddl.py`](../src/bifrost_core/persistence/postgres/ddl.py) `_ensure_tables()` | `bifrost_{dev,stg,prod}` `public.*` |
| Daemon / Account Sync process IPC | [`redis_daemon_state.py`](../src/bifrost_core/persistence/redis_daemon_state.py) — see [DAEMON_IPC_REDIS.md](DAEMON_IPC_REDIS.md) | per-env Redis (`config.redis`) |
| Brokerage Golden Source (IB account / positions / executions) | [`brokerage_ddl.py`](../src/bifrost_core/persistence/postgres/brokerage_ddl.py) | `bifrost_golden_source` `brokerage.*` |
| Market Data (Polygon) | Market Data Plugin (`market.*` / `data_ops.*`) | `bifrost_golden_source` |

## Per-env vs Golden Source

```
bifrost_golden_source
├── market.* / market_analytics.* / data_ops.*   # Polygon Plugin
├── brokerage.*                                  # IB / brokerage adapter
└── flex_ops.*                                   # Flex Query Plugin job queue (Wave 2)

bifrost_{dev,stg,prod}
├── public.*          # strategy_*, gate_safety_*, preferences, watchlist, jobs, bridge tables
└── brokerage.*       # postgres_fdw foreign tables + local views (read JOIN path)
```

Qualified names: [`brokerage_tables.py`](../src/bifrost_core/persistence/postgres/brokerage_tables.py).

Writers open `connect_golden_source()`. Readers stay on the per-env connection and JOIN `brokerage.*` via FDW.

Process IPC (heartbeat / run_status / control) is **not** in PostgreSQL. `_ensure_tables()` does not create the retired `daemon_*` / `account_sync_*` IPC tables.

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

## Commands

```bash
make db-init                 # per-env DDL + brokerage schema + FDW (if golden_source configured)
make db-init-brokerage       # Golden Source brokerage DDL only
make db-init-brokerage-fdw   # + FDW into current per-env DB (needs superuser)
```

See also [BROKERAGE_GOLDEN_SOURCE.md](BROKERAGE_GOLDEN_SOURCE.md) and [DAEMON_IPC_REDIS.md](DAEMON_IPC_REDIS.md).
