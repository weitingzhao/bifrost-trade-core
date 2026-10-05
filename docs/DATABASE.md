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
| Per-env Trade (`settings`, `strategy_*`, `trade_review`, `gate_safety_*`, `preference_*`, `watchlist`, bridge tables, daily snapshots) | [`ddl.py`](../src/bifrost_core/persistence/postgres/ddl.py) `_ensure_tables()` | `bifrost_{dev,stg,prod}` `public.*` |
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
├── public.*          # settings, strategy_*, trade_review, gate_safety_*, preference_*, watchlist,
│                     # bridge tables — 18 tables, every column in the appendix below;
│                     # + the 2 daily snapshot tables (core 0.48.0, see "Daily book snapshots")
├── brokerage.*       # postgres_fdw foreign tables → raw_broker + local views
└── market.*          # postgres_fdw foreign tables → raw_market (ticker, us_market_holiday,
                      # ticker_related) + local view v_us_equity_universe
```

Do not create `flex_ops` on Trade env databases. Flex queue + freshness live in Golden Source `ops_jobs` only.

**Compat shims (Golden Source only, not on Trade DBs)**

| Shim | Canonical target | Notes |
|------|------------------|-------|
| `raw_market.stock_financials` (view) | 6 entity financials tables | **Wave 8** compat UNION view; legacy table dropped |
| `flex_ops.*` (views) | `ops_jobs.job_flex_ingest`, `flex_ingest_freshness` | **DEPRECATED** (Wave 6.3); no new consumers |

**Wave 8 — `settings.active_*_id` FK** (core 0.16.0): `ON DELETE SET NULL` to `strategy_structure`, `gate_safety_strategy`, `strategy_allocation`.

**Flex tokens**: the only source is the K8s Secret (`FLEX_HOST_TOKEN` / `FLEX_SECONDARY_TOKEN`). The
`settings.ib_flex_host_token` / `ib_flex_secondary_token` columns were deprecated in Wave 8 and **dropped in
Wave 11** (core 0.18.0, `migrate_wave11_drop_flex_token_columns()`, which `_ensure_tables()` still runs); DEV has
neither column (checked 2026-10-01).

Qualified names: [`brokerage_tables.py`](../src/bifrost_core/persistence/postgres/brokerage_tables.py), [`market_tables.py`](../src/bifrost_core/persistence/postgres/market_tables.py).

Writers connect to Golden Source with `psycopg2.connect(**_get_golden_source_conn_params(config))`
([`connection.py`](../src/bifrost_core/persistence/postgres/connection.py)) and write `raw_broker.*` (`GOLDEN_*`
names). Readers stay on the per-env connection and JOIN `brokerage.*` via FDW. Trade attribution is per-env
(`trade_execution`, core 0.37.0 as `strategy_instance_execution`, renamed in naming R3, core 0.45.0); nothing writes
Golden Source's `strategy_*` columns any more.

Process IPC (heartbeat / run_status / control) is **not** in PostgreSQL. `_ensure_tables()` does not create the retired `daemon_*` / `account_sync_*` IPC tables.

## Gate safety (1 table)

Safety-boundary config uses metadata scalars + **`params_json`**. Logical grouping (`strategy` / `state` / `intent` / `guard`) is stored in jsonb and validated by `GateParams` pydantic; `get_gates_by_id()` still returns the legacy `config['gates']` dict shape for daemon/API.

| Table | Relationship | Purpose |
|-------|--------------|---------|
| `gate_safety_strategy` | 1 row = 1 boundary set | Metadata + six dims + `params_json` (strategy/state/intent/guard + earnings dates) |

Retired (Wave 9, core **0.17.0**): flat parameter columns on `gate_safety_strategy`, `gate_safety_strategy_earnings_dates`.

**Earnings dates** are stored in `params_json` at `strategy.earnings.dates`, where the daemon's `config['gates']`
(`get_gates_by_id()`) reads them. Over the API they travel in one place, the gate row's top-level `earnings_dates`:
`get_gate_safety_full_by_id()` returns `gates` without `strategy.earnings.dates`, and the writer
(`gate_safety_write`) takes dates only from top-level `earnings_dates`, refusing a non-empty nested
`gates.strategy.earnings.dates` with `ValueError` (HTTP 400). `gate_params.default_gates()` returns the default
`GateParams` in that same `gates` shape, for the API to serve (core **0.32.0**).

Retired (merged into `gate_safety_strategy` in core `0.8.1`): `gate_safety_state`, `gate_safety_intent`, `gate_safety_guard`.

`settings.active_gate_safety_strategy_id` points at the active set. Opportunity / allocation tables keep FK `*_gate_safety_strategy_id`.

## Strategy tables (6 tables) and the Trade entity

The six rule-chain `strategy_*` tables, `strategy_plan`, and the Trade entity: `trade`, its fill attribution
`trade_execution` and `trade_review` (all documented below). **Naming R3 (core 0.45.0)** renamed the entity —
`strategy_instance` → `trade` and `strategy_instance_execution` → `trade_execution` — with their columns
(`strategy_instance_id` → `trade_id`, `allocated_quantity` → `split_quantity`, `trade_review.tags_*` → `tags_*_json`),
sequences, constraints and indexes, in one Owner-run transaction per env (core 0.45.0's `rename_trade_entity.py`;
the SQL is kept in infra `db-steps.d/sql/2026-10-04-r3-*`). For one version (R3 → R4) `public.strategy_instance` and
`public.strategy_instance_execution` were **compatibility views** with the old column names, and so was
`brokerage.instance_allocations`. **Naming R4 (core 0.47.0)** ends that: the code names only the new objects and
reader rows carry only the new keys, and the Owner's R4 step drops the two public views,
`brokerage.instance_allocations` and the frozen `account_execution_instance_allocation` and rebuilds the env views
without `strategy_instance_id` ([`drop_trade_compat.py`](../src/bifrost_core/persistence/postgres/drop_trade_compat.py)):
db-init's FDW step, which would rebuild the env views, stops at `must be owner of foreign server golden_source_server`
in dev / stg / prod (the Job logs `FDW setup skipped`).
`_ensure_tables` creates the new names on a fresh database and **refuses to run** (RuntimeError, before any change)
while `strategy_instance` is still a table.
Every column is in the [appendix](#appendix--public-columns-bifrost_dev-2026-10-01).

| Table | jsonb / notes |
|-------|----------------|
| `strategy_template` | `legs_json`, `params_json`, `characteristics_json`; six `dim_*` enum columns |
| `strategy_structure` | `legs_json`, `meta_json`; optional FK to a template |
| `strategy_opportunity` | `symbols_json`, `entry_conditions_json`; FK to a structure and a default gate set |
| `strategy_allocation` | scalar limits (`max_positions`, `max_bp_pct`); optional gate set |
| `strategy_allocation_opportunity` | N:M junction allocation ↔ opportunity (`sort_order`); both FKs ON DELETE CASCADE |
| `trade` | (was `strategy_instance`) one trade opened under an opportunity in one account — see below |
| `strategy_plan` | `legs_json`, `source_json`; structured trade plans — **advisory, no execution consumer (D10)** |

**Legs and `params_json` (TD-44, core 0.41.0).** There are two kinds of leg, not three copies of one:
`strategy_template.legs_json` and `strategy_structure.legs_json` hold **abstract slots**
(`{role: underlying|call|put, direction: long|short, option_right: ''|C|P, quantity ≥ 1, quantity_default?, sort_order}`;
a structure leg also carries `strike` / `expiration`, never filled on any env and deprecated), validated by
`gate_params.AbstractLeg` on every write; `strategy_plan.legs_json` holds **concrete contracts** (`PlanLeg`, below).
`gate_params.abstract_leg_to_plan_leg(leg, symbol=, expiry=, strike=)` is the one mapping (long → buy, short → sell,
option_right → right, quantity → ratio; the option `contract_key` in the positions format). Two columns are named
`params_json` and mean different things: `strategy_template.params_json` is an **array of parameter definitions**
(`{meta_key, display_label, param_kind, default_value_text, sort_order}`, served by the API as `meta_params`);
`gate_safety_strategy.params_json` is **one GateParams object**. No column is renamed (Owner 2026-10-03).

**`strategy_opportunity.scope_type` (TD-71, core 0.41.0).** `watchlist_stk` · `explicit_symbols` · NULL, held by
CHECK `strategy_opportunity_scope_type_ck` and by core (`''` is stored as NULL; anything else is 400).
`symbols_json` is what the rule covers either way — `scope_type` only says where the symbols came from — and
`watchlist_stk` needs at least one symbol (core refuses an empty one).

### `trade` (was `strategy_instance`; renamed in core **0.45.0**)

One position the desk actually opened under an opportunity, in one IB account. Executions are attributed to
it, plans that were filled point at it, and Review keeps one verdict per trade. DDL:
[`trade_ddl.py`](../src/bifrost_core/persistence/postgres/trade_ddl.py).

| Column | Meaning |
|--------|---------|
| `trade_id` | PK (bigserial, sequence `trade_trade_id_seq`; constraint `trade_pkey`). Was `strategy_instance_id` |
| `strategy_opportunity_id` | NOT NULL, FK → `strategy_opportunity` **ON DELETE RESTRICT** (an opportunity with instances cannot be deleted) |
| `account_id` | IB account. An execution can be allocated to the instance only when its account matches |
| `opened_at` | When the position was opened (NOT NULL). A filled plan's `filled_at` reads as this value (not stored since 0.41.0) |
| `label` | Free text |
| ~~`notes`~~ | **Dropped (TD-73).** Not read or written since 0.43.0 and not created on a fresh database; existing databases lose it in the Owner db-step after that release (infra `scripts/release/db-steps.d/2026-10-03-td43-td73-drop-columns.md`). A trade's notes live in the Research journal (`journal.note`, ref `inst`) |
| `created_at` / `updated_at` | Row timestamps |

Indexes: `trade_opportunity_id (strategy_opportunity_id)`, `trade_account_opened (account_id, opened_at)`; FK
`trade_strategy_opportunity_id_fkey`; UNIQUE `trade_id_account_uq (trade_id, account_id)`. Nothing in the schema limits an
opportunity/account pair to one open instance, and nothing should: PROD has pairs with two open at once (rolls,
parallel trades).

**State is derived, not stored (TD-43, core 0.41.0).** `list_instances` (`GET /strategies/instances`) adds `state`
and `closed_on`, computed by [`instance_state.py`](../src/bifrost_core/monitor/reader/instance_state.py) from the
instance's OPT fills (whole fills their quantity, split fills the instance's share; grouped per `contract_key`):
`no_fills` (no option fill attributed), `open` (a leg open and not past expiry), `expired` (every open leg past its
expiry with no closing fill — counted as closed, `closed_on` = the last expiry), `closed` (every leg flat,
`closed_on` = the last day a leg went flat). One rule for Rules, Review, Risk › Limits and the research MCP.

Referenced by: `trade_execution (trade_id, account_id)` (`trade_execution_trade_fk`, ON DELETE RESTRICT; the
UNIQUE `trade_id_account_uq` exists for it), `strategy_plan.trade_id` (`strategy_plan_trade_id_fkey`, **RESTRICT**
since 0.41.0; SET NULL before), `trade_review.trade_id` (`trade_review_trade_id_fkey`, **RESTRICT** since 0.41.0,
CASCADE before; UNIQUE `trade_review_trade_id_key`), and until the R4 step the frozen
`account_execution_instance_allocation.strategy_instance_id`. So a trade a plan was filled by, or one with a review,
cannot be deleted (`delete_instance_strict` answers 409 naming which).

**Compatibility view `public.strategy_instance` (R3 → R4 only; dropped by the R4 step).** `SELECT trade_id AS
strategy_instance_id, strategy_opportunity_id, account_id, opened_at, label, created_at, updated_at FROM trade`.
Made by the rename step, never by `_ensure_tables`.

### `trade_execution` (core **0.37.0** as `strategy_instance_execution`; renamed in **0.45.0**)

Which trade a fill belongs to, in this environment (TD-09). Before 0.37.0 this was two columns on Golden
Source's raw rows, shared by all three environments, plus `account_execution_instance_allocation` for splits
(frozen, dropped by the R4 step).

| Column | Meaning |
|--------|---------|
| `trade_execution_id` | PK (sequence `trade_execution_trade_execution_id_seq`). Was `strategy_instance_execution_id` |
| `account_id`, `exec_id` | The fill. A TWS row and its Flex twin share `exec_id`, so one row attributes both; a raw row without `exec_id` cannot be attributed |
| `trade_id` | NOT NULL. With `account_id`, FK `trade_execution_trade_fk` → `trade (trade_id, account_id)` **ON DELETE RESTRICT**: the fill's account must be the trade's. Was `strategy_instance_id` |
| `split_quantity` | NULL = the whole fill. Otherwise this trade's share of a fill split (signed like the fill; CHECK `trade_execution_qty_ck` ≠ 0). Was `allocated_quantity` (TD-82: "allocation" is the capital rule only) |
| `created_at` / `updated_at` | Row timestamps |

Constraints: `trade_execution_pkey`; UNIQUE `trade_execution_uq (account_id, exec_id, trade_id)`; partial UNIQUE
`trade_execution_whole_uq (account_id, exec_id) WHERE split_quantity IS NULL` (one whole-fill row per fill); index
`trade_execution_trade_ix (trade_id)`. A fill is attributed whole or split, never both — the writers keep that
(`accounts.py`), not a trigger. The opportunity is not stored: it is the trade's. Constants:
`brokerage_tables.TRADE_EXECUTION` (its alias `INSTANCE_EXECUTION` went in 0.47.0).

Read through the per-env views (built in `brokerage_views._create_brokerage_views(..., env=True)` with the FDW
tables): `brokerage.executions` / `executions_final` / `executions_fly` take `trade_id` from the whole-fill row and
`strategy_opportunity_id` from its trade, and rename IB's columns `ib_trade_id` / `ib_related_trade_id` (TD-13: one
`trade_id`; core 0.45.0–0.46.x also carried `strategy_instance_id` = `trade_id` right after it).
`brokerage.executions_tws` is every TWS raw row the same way (the `tws_raw` scope); `brokerage.trade_fill_splits`
gives the split rows once per raw representation (Flex id, TWS −id, journal −(1e9+id)): `account_id`,
`account_executions_id`, `trade_id`, `quantity` (float8), `exec_id` (`brokerage_tables.TRADE_FILL_SPLITS`;
its alias `INSTANCE_ALLOCATION` went in 0.47.0). Core 0.45.0–0.46.x also made `brokerage.instance_allocations` over
it with core 0.44.0's columns; from 0.47.0 the view rebuild drops it by name (`brokerage_views.RETIRED_ENV_VIEWS`) and the
R4 step does so in dev / stg / prod.
`setup_fdw_foreign_tables` refuses to run before `trade_execution` exists.

**Compatibility view `public.strategy_instance_execution` (R3 → R4 only; dropped by the R4 step).** `SELECT trade_execution_id AS
strategy_instance_execution_id, account_id, exec_id, trade_id AS strategy_instance_id, split_quantity AS
allocated_quantity, created_at, updated_at FROM trade_execution`. Auto-updatable; core 0.44.0's whole-fill upsert
`INSERT … ON CONFLICT (account_id, exec_id) WHERE allocated_quantity IS NULL DO UPDATE …` works through it —
PostgreSQL maps the arbiter onto `trade_execution_whole_uq` (rehearsed on 16 and 17, insert and conflict arms).

### `strategy_plan` (core **0.22.0**)

What the desk intends, so that afterwards there is something to compare the
fill against. Nothing reads it to act: the daemon and the gateway do not know
it exists. Orders are placed in TWS.

| Column | Meaning |
|--------|---------|
| `legs_json` | Leg array: `{side: 'buy'\|'sell', sec_type: 'OPT'\|'STK', right: 'C'\|'P'\|null, strike: number\|null, expiry: 'YYYY-MM-DD'\|null, ratio: int ≥ 1, contract_key: 'SYM\|OPT\|YYYYMMDD\|STRIKE\|R'\|null, mid_at_plan: number\|null, quote_asof: ISO\|null}`. An `OPT` leg needs right, strike and expiry |
| `target_kind` / `target_value` | Take profit: `credit_pct` = per cent of the premium bought back (50 = 50%); `option_price` = combination price; `underlying_price` = price of the underlying |
| `stop_kind` / `stop_value` | Stop: `credit_multiple` = multiple of the premium (2 = −2× credit); the other two as above |
| `exit_by` | Latest planned exit date |
| `source_kind` / `source_ref` / `source_json` | Where the plan came from, and the provenance chain as it stood: `[{kind, text, ref?, to?}]` |
| `status` | `draft` → `intended` → `filled`, or `cancelled` from either of the first two. No delete |
| `expires_at` | **`expired` is not a stored status**: `status='intended'` with `expires_at < now()` reads `effective_status='expired'` |
| `trade_id` | The trade the plan turned into (was `strategy_instance_id`, renamed in 0.45.0), FK `strategy_plan_trade_id_fkey` ON DELETE RESTRICT; index `strategy_plan_trade` (partial, non-null). CHECK `strategy_plan_filled_instance_ck` (name kept): `(status = 'filled') = (trade_id IS NOT NULL)` — `link_fill` sets both |
| `filled_at` | **Not a column (TD-43).** Reads return the linked trade's `opened_at` (`LEFT JOIN trade`; only a filled plan has one, so every other plan reads null) and moving the open moves it. Not written since 0.41.0, not named at all since 0.43.0 and not created on a fresh database; existing databases lose the column in the Owner db-step after 0.43.0 (infra `scripts/release/db-steps.d/2026-10-03-td43-td73-drop-columns.md`) |
| `parent_strategy_plan_id` | The plan this one rolls. An intended plan is frozen — roll it rather than edit it |

State machine and reads: [`strategy_plan.py`](../src/bifrost_core/monitor/reader/strategy_plan.py).
`intend` requires at least one leg and at least one of target / stop / `exit_by`
— without a planned exit there is nothing to measure adherence against.

### `trade_review` (core **0.26.0**)

The trader's own verdict on one instance, read by Review › Queue and Review ›
Single trade (design Rev .110). One row per instance; an instance is *awaiting*
until `reviewed_at` is stamped, and the Review menu badge counts closed
instances without one. Whether an instance is closed is the fills' to say — the
caller decides when a review may be confirmed. No delete: reopening clears the
stamp and keeps the tags.

| Column | Meaning |
|--------|---------|
| `trade_id` | UNIQUE (`trade_review_trade_id_key`), FK `trade_review_trade_id_fkey` → `trade` ON DELETE RESTRICT (CASCADE before 0.41.0: a review is never deleted with its trade). Was `strategy_instance_id` |
| `tags_added_json` | Tags the rules missed, as the trader wrote them (jsonb string array). Was `tags_added` |
| `tags_dropped_json` | Keys of derived tags the trader says do not apply (jsonb string array). Was `tags_dropped` |
| ~~`note`~~ | **Dropped (TD-73).** Not read or written since 0.43.0 (a `note` key is refused) and not created on a fresh database; existing databases lose it in the Owner db-step after that release. A trade's notes live in the Research journal |
| `reviewed_at` | Stamped on confirm (a second confirm keeps the first stamp); NULL = awaiting |

Reads and the upsert: [`trade_review.py`](../src/bifrost_core/monitor/reader/trade_review.py).

Retired (Wave 9): `strategy_dim` (→ six `dim_*_t` enum types + read-only catalog), `strategy_template_leg`, `strategy_structure_leg`, `strategy_opportunity_symbol`, `strategy_opportunity_entry_condition`.

**Dimension enums.** `strategy_template.dim_*` and `gate_safety_strategy.dim_*` are typed `dim_direction_t`,
`dim_structure_t`, `dim_coverage_t`, `dim_risk_t`, `dim_volatility_t` and `dim_time_t`. Their labels are the 25 codes the
retired `strategy_dim` table held, and [`strategy_dim_catalog.py`](../src/bifrost_core/monitor/reader/strategy_dim_catalog.py)
lists exactly those. The API validates against the catalog and Postgres validates against the type, so the two must hold the same set.
`ensure_dim_enum_types()` creates a missing type from the catalog but never alters one that exists. If an existing type
disagrees with the catalog, it logs the difference. To add a code, run `ALTER TYPE … ADD VALUE` in all three envs and add the
catalog entry in the same change. Until core 0.23.0 the catalog held a different, never-applied set.

**Wave 9 — strategy collapse** (core **0.17.0**): one-shot migration `migrate_wave9_strategy_collapse()` in [`wave9_migrations.py`](../src/bifrost_core/persistence/postgres/wave9_migrations.py).

## Preference: instrument class (core **0.27.0**)

### `preference_instrument_class`

What kind of security a stock-like holding is — `stock`, `fixed_income` or
`cash_like`. IB books a bond or T-bill ETF as STK and no vendor field says which
funds are fixed income, so the Owner registers it once per instrument (trade
design Rev .119, Owner-approved 2026-09-30). It is a property of the security,
not of an account, unlike the category tags. Positions → Shares types by it, and
the book's Δ (stocks + options) leaves fixed-income and cash-like shares out.

| Column | Meaning |
|--------|---------|
| `preference_instrument_class_id` | PK |
| `contract_key` | UNIQUE — the key positions, watchlist and category tags use (STK: `SYMBOL\|STK\|\|\|`, e.g. `SGOV\|STK\|\|\|`). No FK: a registration outlives the holding |
| `instrument_class` | `stock` · `fixed_income` · `cash_like` (CHECK; text rather than an enum type, so a fourth class is one constraint change) |
| `note` | Optional: why it is registered so |

An instrument with no row is **unregistered** and reads as a stock; nothing
infers a class from the category. The positions read (`get_accounts_from_tables`)
LEFT JOINs it and adds `instrument_class` to each position that has one.

**Order of release:** the positions read joins this table only where it exists
(`to_regclass`, checked per read), so an api on core ≥ 0.27.0 ahead of the DDL
reads every position unclassified rather than failing. Classes appear once the
table is created — create it before the frontend that reads them ships.

Reads and writes: [`instrument_class.py`](../src/bifrost_core/portfolio/reader/instrument_class.py).

## Preference: saved searches (core **0.28.0**)

### `preference_saved_search`

A page's filters kept under a name — the Finder's smart folders (trade design
Rev .139, Owner-approved 2026-10-01). Plans' **Save as list** writes one; the
sidebar lists them on every page and a click opens the page with that scope.
Stored server-side so they follow the operator to another machine.

| Column | Meaning |
|--------|---------|
| `preference_saved_search_id` | PK |
| `owner` | `'operator'` today — Trade has no sign-in, so there is one operator (Owner 2026-10-01); the column a future sign-in keys on |
| `route` | The app path the scope belongs to, e.g. `/trade/plans` |
| `label` | The name in the sidebar (auto-named by the page, e.g. `AMD · all`) |
| `state_json` | The page's own scope (status, accounts, symbol, tokens …); opaque to core |

`UNIQUE (owner, route, label)`: saving a label again on the same page replaces
its scope. Reads guard on `to_regclass`, so an api ahead of the DDL lists none.

Reads and writes: [`saved_search.py`](../src/bifrost_core/monitor/reader/saved_search.py).

## Daily book snapshots (core **0.48.0**, W4)

The broker tables (`brokerage.positions`, `brokerage.account`) hold the current book only, so
yesterday's positions and NAV exist nowhere unless they are kept. These two tables keep them, one
session at a time, from the day they ship: they cannot be backfilled. Phase 0 W4; the position
table was approved 2026-09-30, the NAV table 2026-10-05 (Owner, "加账户级 NAV 行"). DDL:
[`snapshot_ddl.py`](../src/bifrost_core/persistence/postgres/snapshot_ddl.py), run by `_ensure_tables`
(`CREATE … IF NOT EXISTS` only; no existing object changes). Writer: the nightly job
[`portfolio/snapshot`](../src/bifrost_core/portfolio/snapshot/daily.py) (`python -m bifrost_core.portfolio.snapshot
capture|enrich`, per-env CronJob); nothing else writes them.

### `position_snapshot_daily`

One row per session, account, contract and trade. Natural key UNIQUE NULLS NOT DISTINCT
`position_snapshot_daily_uq (snapshot_date, account_id, contract_key, trade_id)`; index
`position_snapshot_daily_trade_ix (trade_id, snapshot_date) WHERE trade_id IS NOT NULL`.

| Column | Meaning |
|--------|---------|
| `position_snapshot_daily_id` | PK (bigserial) |
| `snapshot_date` | The New York session date |
| `account_id`, `contract_key` | The broker position (`brokerage.positions` keys) |
| `trade_id` | The trade this row's share belongs to. **No FK**: history must not stop a trade from being deleted. NULL = the part of the position no trade's fills explain (a position with no attributed fill is that row alone) |
| `symbol`, `sec_type`, `expiry` (date), `strike`, `option_right` | Copied from the position, so a row reads without the contract still existing |
| `position_qty` | The broker's whole position on the contract (signed) |
| `trade_qty` | This row's share: the trade's net fills on the contract (`get_position_instance_attribution`'s `open_qty_est`); the NULL-trade row takes the remainder, so a position's rows add up to `position_qty` |
| `avg_cost` | The broker's average cost (per contract for options, as IB reports it) |
| `mark`, `mark_source` | `quote_live`: the fresh `contract_quote_live` last (else mid) at capture; `vendor_eod`: filled by enrich from the vendor's session close (option `day_close`, stock daily close) where capture had none |
| `underlying_close` | The underlying's daily close for the session (market-data plugin `stock_daily`) |
| `delta`, `gamma`, `vega`, `theta`, `iv` | Vendor EOD values (`raw_market.option_snapshot`, the session's 16:00 anchor, via the plugin); NULL for stock rows and for a contract the vendor has no row for |
| `greeks_asof` | The vendor snapshot's `snapshot_ts` |
| `positions_updated_at` | `brokerage.positions.updated_at` when captured — shows a stale broker sync |
| `captured_at` | Row insert time |

### `account_nav_daily`

One row per session and account (UNIQUE `account_nav_daily_uq (snapshot_date, account_id)`):
`account_nav_daily_id` (PK), `snapshot_date`, `account_id`, `net_liquidation`, `total_cash`,
`buying_power` (from `brokerage.account`), `account_updated_at` (that row's `updated_at`),
`captured_at`. The start-of-range balance that time-weighted return and Sharpe need (Performance
reads `not recorded` without it; the frontend is not wired to it yet).

**Write rule.** `capture` (after the close) inserts with `ON CONFLICT DO NOTHING`: the first
capture of a session is kept and a rerun never rewrites it. It refuses (exit 1, nothing written)
when the broker has open positions but the attribution read returned none. `enrich` (evening)
only fills NULLs. Weekends and full-day NYSE holidays (`market.us_market_holiday`) are skipped.
The runtime role `trade_app_<env>` reads and writes both tables through bifrost's default
privileges in `public` (TD-85); no GRANT is needed.

## §6 Schema changelog (Wave 1–15)

| Wave | Core version | Change |
|------|--------------|--------|
| Wave 1 | 0.8.1+ | Merge `gate_safety_state/intent/guard` into `gate_safety_strategy` flat columns |
| Wave 2 | 0.11.0 | Fold `strategy_template_param/characteristic`, `strategy_structure_meta` into parent jsonb |
| Wave 3 | 0.12.0 | Drop `strategy_history`; extend `raw_broker.transactions` UNIQUE |
| Wave 4 | 0.13.0 | `ops_audit_log` partitioned (later dropped Wave 6) |
| Wave 5 | 0.14.0 | Trade Celery / `job_*` queues retired → Plugin `ops_jobs` |
| Wave 6 | 0.15.0 | Drop `ops_audit_log`; audit → platform-api |
| Wave 8 | 0.16.0 | `settings.active_*_id` FK ON DELETE SET NULL; Flex token columns DEPRECATED |
| Wave 9 | 0.17.0 | Collapse strategy child tables + gate flat cols → jsonb; `strategy_dim` → enum + catalog |
| Wave 10 | 0.17.2 | Remove Wave 1 `_upgrade_gate_safety_strategy` DDL path; `ensure_dim_enum_types()` from catalog; CREATE uses `dim_*_t` |
| Wave 11 | 0.18.0 | DROP `settings.ib_flex_host_token` / `ib_flex_secondary_token`; Flex Plugin Secret-only token path |
| Wave 12 | 0.22.0 | Add `strategy_plan` (structured trade plans; advisory, no execution consumer) |
| — | 0.23.0 | No DDL. `strategy_dim_catalog` changed to the live `dim_*_t` labels, which are the Wave 9 `strategy_dim` codes. `ensure_dim_enum_types()` now logs any drift |
| Wave 13 | 0.24.0 | `migrate_wave13_reconcile_legacy_schema()`: pre-split leftovers `IF NOT EXISTS` can't reach — `strategy_portfolio_*` sequence / PK / FK / index names → `strategy_allocation_*`; `market_streams_symbol_order_pkey` → `preference_market_streams_symbol_order_pkey`; add FK `strategy_allocation_opportunity.strategy_opportunity_id` → `strategy_opportunity` ON DELETE CASCADE (left NOT VALID with a warning if orphans exist); `settings.flex_*_range_days` SET NOT NULL; DROP `settings.ib_primary_account_id` / `stream_primary_account_id`; DROP redundant `watchlist_contract_key`. Each step checks first and is a no-op on a converged DB |
| — | 0.26.0 | Add `trade_review` (one review record per strategy instance: tags added / dropped, `reviewed_at`) |
| — | 0.27.0 | Add `preference_instrument_class` (stock / fixed_income / cash_like per `contract_key`); the positions read LEFT JOINs it where the table exists (unclassified otherwise) |
| — | 0.28.0 | Add `preference_saved_search` (a page's scope under a name, one operator); no DDL for the new deletes — `delete_plan` (drafts) and `strategy_rules_delete` (opportunity · allocation · gate set, refused while in use) |
| — | docs only (2026-10-01) | No DDL. DATABASE.md corrected against the live DEV schema (debt TD-35): the Flex token columns are recorded as dropped (Wave 11), not pending; `jobs` removed from the per-env table list (retired Wave 5); "Strategy tables" lists the 7 `strategy_*` tables that exist (adds `strategy_allocation_opportunity`) and documents `strategy_instance`; `market.us_market_holiday` and `market.ticker_related` documented; `connect_golden_source()` (deleted by TD-59) no longer cited; per-table column appendix added. BROKERAGE_GOLDEN_SOURCE.md documents the `raw_broker` non-vendor columns |
| — | 0.32.0 | No DDL. Wave 9 leftovers deleted (TD-58): `structure_type_config` / `structure_type_config_write` (they read and wrote `strategy_structure_type*` tables no DDL creates); `structure_type_schema` keeps only `build_schema_from_legs` + `validate_legs` (schema now required); the `to_regclass('public.strategy_dim')` probe — dims come from `strategy_dim_catalog` only; the `strategy_template_leg` fallback in `get_template_legs`; `gate_safety._load_earnings_dates`; `template_config_write.create_dim/update_dim/delete_dim`. `legacy_account_executions_id` documented as a historical map column. Gate earnings dates travel only as top-level `earnings_dates` (TD-72, see Gate safety); new `gate_params.default_gates()`. Affected downstreams: **api** (delete the `/strategies/dims` POST/PUT/DELETE routes that call the removed `*_dim` writers; serve `default_gates()`), **worker** (no code change: `get_gates_by_id()` shape unchanged) |
| — | 0.32.1 | No DDL. Position attribution (`get_position_instance_attribution`) names the structure and its template apart (TD-41): new `strategy_structure_name` (`strategy_structure.name`) and `template_code` (joined `strategy_template.template_code`); `structure_type` stays one version as an alias of the name — `/strategies/structures` used the same key for the template code. Affected downstreams: **frontend** (Positions groups and filter read `template_code`), **api** (no code change) |
| — | 0.33.0 | No DDL. Write outcomes (TD-15), additive: `monitor/reader/errors.py` adds `WriteError` and its four outcomes `WriteNotFound` / `WriteConflict` / `WriteInvalid` / `WriteFailed` (each with `reason`; `WriteFailed.unavailable` is True when Postgres / Golden Source was not configured or not reachable, False when a statement failed); `RuleInUseError` and `PlanRuleError` become `WriteConflict` subclasses and `SavedSearchError` a `WriteInvalid` (all still `ValueError`). New writers return the row and raise an outcome instead of answering a bool: `patch_instance` · `patch_allocation` · `patch_opportunity` · `patch_template` · `patch_gate_safety` · `patch_structure` · `patch_plan` (an intended plan may change `expires_at` only) · `patch_review` · `patch_position_category` · `patch_instrument_class` · `patch_execution` (attribution only) · `patch_watchlist_item` · `upsert_watchlist`; strict deletes answer `{"deleted": "hard"\|"soft", …}`: `delete_template_strict` · `delete_structure_strict` (soft) · `delete_opportunity_strict` · `delete_allocation_strict` · `delete_gate_safety_strict` · `delete_plan_strict` · `delete_saved_search_strict` · `delete_instance_strict` (refused while executions are split-allocated to it, or attributed to it on Golden Source `raw_broker.executions_raw_*`; Golden Source unreachable refuses too) · `delete_position_category_strict` · `delete_instrument_class_strict` · `delete_execution_strict` (refused while an option/stock link names it; the commission row goes only when no other raw row carries its `exec_id`) · `delete_option_stock_link_strict` · `delete_watchlist_strict`. Existing writers keep their signatures and results for one release, with one behaviour fix: `add_watchlist` (POST /watchlist) keeps stored values for columns passed as None on a re-add — it used to NULL `category_id` / `display_label` / `source` — and takes `clear=(…)` for an explicit NULL. Affected downstreams: **api** (map the outcomes to 404 / 409 / 422 / 503; PATCH and strict DELETE routes call the new writers; POST /watchlist must pass `clear=("category_id",)` when the body sends `category_id: null`, or call `upsert_watchlist` with the fields set; raise the floor to `bifrost-core>=0.33.0`), **worker** (none: it calls none of these writers) |
| — | 0.33.1 | No DDL. One answer to which environment a process serves (TD-52): new `config.profile.deployment_profile` — `ops.control_profile`, then `BIFROST_OPS_CONTROL_PROFILE`, then `BIFROST_ENV`, then the file name; `stg` is a profile everywhere (`ops_profile_from_config` mapped it to None, `config_profile_from_resolved_path` knew only dev and prod). Affected downstreams: **api** (every app reports its profile through it), **infra** (pods mount `/app/config/runtime.yaml` and set their own `BIFROST_ENV`) |
| — | 0.33.2 | No DDL. Connections (TD-46): every reader / writer that opens its own connection from a status config — the nine `_conn_from_config` seams, `accounts.py` (12 connects), `market.py`, `settings.py`, `option_stock_link._connect` — goes through `write_support.open_conn`, which sets `connect_timeout=10` (the value the Golden Source connects and `write_connection` already used). Behaviour change: a per-env database host that does not answer now fails after 10 s, with the same result as any other connect failure (None / False, logged), instead of hanging. `StatusReader` and the daemon `PostgreSQLSink` keep their own connects. Imports (TD-47): `monitor.reader` re-exports lazily, so `import …persistence.postgres.ddl` loads 6 core modules (was 49) and a fresh `import …portfolio.reader.accounts` no longer fails on a cycle; public names unchanged. Affected downstreams: none (no API change) |
| — | 0.34.0 | No DDL, no persisted format change. Black-Scholes (TD-42): `pricing.black_scholes` gains the erf closed form (`norm_cdf`, `norm_pdf`, `d1`, `d2`, `price`, `erf_delta`, `erf_gamma`, `vega`, `theta`, `greeks`, `prob_itm`, `implied_vol(convention=IV_POSITIONS_MODEL | IV_RESEARCH)`, `strict=` for the api's raw-formula behaviour) and `RATE_*` constants naming each surface's risk-free rate (0 research GEX, 0.04 Positions model, 0.043 Symbol chain UI, 0.045 research greeks / screener, 0.05 daemon) — not unified; the Positions model now calls it and is bit-identical; `delta` / `gamma` (py_vollib) unchanged; the uncalled `calculate_greeks` fills theta / vega (were 0.0). contract_key (TD-25): new `portfolio.contract_key` (`stk_key`, `opt_key`, `execution_opt_fields`, `legacy_tws_local_symbol`, `tws_execution_opt_key`, `osi_local_symbol`, `read_fallback_opt_key`); positions sync, both execution writers, the read fallback and the join variants build keys there, byte-identical (golden test); the read-time fallback for rows stored without a key now prints a fractional strike in full (`82.5`, was `82`), integral strikes stay `80`; nothing it builds is written. Removed (TD-78): `monitor.reader` `write_ohlc_bars_to_db` / `write_stock_bars` / `delete_stock_bars_for_symbol` and `market_write_client.post_bars_ingest` / `delete_bars` (no caller anywhere). Public aliases (TD-20): `connection.get_conn_params` / `get_golden_source_conn_params`, `ddl.ensure_tables`, `StatusReader.config` (read-only); the private names stay. Affected downstreams: api — may switch `research/routers/greeks.py` and `screener.py` to core pricing (map its right rule to "C"/"P", `strict=True`, `IV_RESEARCH`, `RATE_RESEARCH`; the golden test shows the calls) and move to the public aliases and `reader.config`; worker — none required (`calculate_greeks` / `delta` / `gamma` imports unchanged; may use the public `get_conn_params`); Flex — none required (may use the public connection names) |
| — | 0.35.0 | No DDL, no persisted format change. **Signed quantity (TD-30), public behaviour change:** one rule, `portfolio.signed_qty` (`signed_qty_sql(alias)` / `signed_qty(source, side, quantity)`): SELL / SLD / S → −\|q\|, anything else → +\|q\|, NULL stays NULL, whatever the source or stored sign. `quantity` on `GET /executions` (scopes all / performance_book / on_the_fly, with or without opt pairs), `GET /executions/link-candidates`, option-stock link `stock_quantity` and stock-link candidate `quantity` is now negative for every sell (Flex and journal sells, stored negative, used to read back positive; TWS sells, stored positive, were passed through); buys unchanged; `source_scope=tws_raw` still returns the stored TWS value. Amounts do not move: attribution `open_qty_est` equals the old attribution expressions (db test), Performance / instance summary / Instance exec net P&L / win-rate risk are identical on old- and new-signed rows, and option-stock `slippage_vs_close` is computed on \|q\| (what the old column gave). The split-allocation sum check expects −\|q\| for a TWS sell (what the execution form sends; it was refused). **Daemon sink (TD-45):** `PostgreSQLSink` connects without DDL (no `_ensure_tables` / `ensure_brokerage_schema`) and never terminates other backends; `connection.release_pg_locks_for_tables` is deleted; a missing table fails the write and logs an error; schema comes only from the db-init Job (`ensure_tables` / `ensure_brokerage_schema` stay for it). **Liveness (TD-76):** `monitor.self_check.daemon_alive_threshold_sec` = max(35, 3 × heartbeat interval, 5–120 s, default 10 s) and `is_daemon_alive`. **Executions fetch:** `portfolio.gateway_fills` maps the IB Gateway plugin's fills (`account` / `shares` / `ts`) to the writer's row (`account_id` / `quantity` / `time`, `source = tws_client`), refusing fills without account / quantity and option fills without expiry / strike / right (the plugin does not send them); the six NULL rows from 2026-08-08 are left. **TD-48:** `create_allocation` / `update_allocation` and `create_opportunity` / `update_opportunity` raise `WriteInvalid` for a limit or gate id they cannot store (was stored as NULL). Affected downstreams: frontend — remove its sell-sign compensations for `/executions` and the link rows in the same release; api 0.3.3 — needs core ≥ 0.35.0 (`is_daemon_alive`, `gateway_fills`, the 400s); worker 0.2.2 — the daemon now needs the db-init Job to have run before it starts writing; Flex — none |
| — | 0.35.1 | No DDL. Config (TD-53/54/79): connection settings are env, then YAML, then defaults for Postgres, Golden Source (per field, falling back to the Trade database; the name never falls back) and Redis (the IB bus per field); daemon gate defaults come from `GateParams`, not `config.yaml.example` beside the config; `normalize_server_config` requires monitor / account (`account_port`, legacy `trading_port` kept as an alias) / research / market ports and drops the retired five; the `ib` block is optional. Old configs still load. Affected downstreams: **api** (reads `account_port`), **worker** (no change), **infra** (dead ports and IB blocks removable from overlays in a later release) |
| — | 0.36.0 | No DDL. **Structures carry no `structure_subtype` (TD-41), public response change:** `get_structure_by_id` and the structure list no longer return `structure_subtype` (always `NULL`; no such column) or `structure_subtype_label` (it repeated `template_display_name`). The writer no longer reads a `structure_subtype` from the payload: a bare `structure_type: covered_call` still resolves to `covered_call_otm`, any other template is named by `strategy_template_id` or its code. Affected downstreams: **api** (0.4.0 drops the request field), **frontend** (stopped sending and reading it in 678e0d2e) |
| — | 0.36.1 | No DDL. `set_instrument_class(..., keep_note=True)`: the default keeps a stored note when none is sent (unchanged); `keep_note=False` is a full replace, so the row becomes what was sent and no note clears it (TD-15, used by PUT /instrument-classes from api 0.6.0). Additive; no other caller. |
| — | 0.37.0 | **DDL (TD-09): per-env strategy attribution.** Add `strategy_instance_execution` and `strategy_instance_id_account_uq` UNIQUE `(strategy_instance_id, account_id)` on `strategy_instance` (`STRATEGY_INSTANCE_EXECUTION_DDL`, run by `_ensure_tables`). The per-env views read attribution from it (`_create_brokerage_views(env=True)`; new env-only views `brokerage.executions_tws`, `brokerage.instance_allocations`); `setup_fdw_foreign_tables` refuses to run before the table exists. Writers (`patch_execution`, `update_one_execution`, `insert_one_execution`, `batch_update_execution_strategy`, the deletes) write only the table — Golden Source's raw `strategy_*` columns and `account_execution_instance_allocation` are frozen (not cleared). Behaviour changes: an opportunity without an instance is refused (`patch_execution` WriteInvalid; the bool writers answer False / None, the batch 0); an opportunity sent with an instance must be the instance's; the opportunity read back is always the instance's; a split quantity of 0 is refused; attributing a fill attributes its TWS / Flex twin too; `update_one_execution` refuses to move an attributed fill to another account; deleting a raw row keeps the attribution while its twin remains; `count_attributed_executions` / `delete_instance_strict` count this env's table. `brokerage_tables`: new `INSTANCE_EXECUTION`, `EXECUTIONS_TWS`, `LEGACY_INSTANCE_ALLOCATION`, `BROKERAGE_ENV_VIEWS`; `INSTANCE_ALLOCATION` now names the read view. One-off move: `td09_attribution.migration_sql` / `scripts/db/td09_migrate_attribution.py` (one transaction per env: DDL, instance #3 → U8829175, empty and reload from the Golden Source columns and the old split table under the approved rules, optional views; ROLLBACK unless `--commit`). Affected downstreams: **api** (raise the floor to `bifrost-core>=0.37.0`; PATCH /executions/{id}/attribution answers 400 for an opportunity without a trade), **frontend** (a fill links to a trade: Link execution and the execution form require one), **worker** / Flex / Research (none) |
| — | 0.38.0 | No DDL. **'Trade' is the instance (TD-19), additive + one behaviour change.** Performance (`get_performance_stats`, `get_performance_instance_summary_only`): `fill_count` beside every `trade_count` that counts fills — `summary`, `realized_by_account` / `_sec_type` / `_account_and_sec_type` / `_strategy_opportunity` / `_strategy_instance`, `calendar`, and the non-option rows of `calendar_by_sec_type`; the option rows of `calendar_by_sec_type` count closed option pairs and get `pair_count` instead. Win rate by structure: `total_trades` beside `total_instances`. `trade_count` and `total_instances` stay one version. **Behaviour change:** `summary.win_rate` = wins ÷ (wins + losses), the fills that realized a gain or a loss — it was wins ÷ every fill, opening fills included, and read low (the calendar rows already used the closing rule). Affected downstreams: **api** (none beyond the floor `bifrost-core>=0.38.0`; no response model filters these keys), **frontend** (read `fill_count` / `total_trades` with the old key as fallback), Research / worker (none: they read neither) |
| — | 0.39.0 | No DDL, no Redis key or value change. **Daemon names (TD-75), Python identifiers only:** `redis_health_keys.BIFROST_HEALTH_DAEMON_STRATEGY_TRADING` (value `bifrost:health:daemon_strategy_trading`, unchanged — a live Redis key) replaces `BIFROST_HEALTH_DAEMON_TRADING_ENGINE`, and `postgres_sink.TradingDaemonSink` replaces `PostgreSQLSink`; both old names stay as aliases for this version only. Deleted (no reader in any repo): `BIFROST_OPS_TRADING_ENGINE_META` and `config.yaml_config.daemon_trading_console_stream_key` (with its `config.startup` re-export). **Flex range days (TD-74), public response change:** `get_ib_config` (settings reader and `StatusReader`) no longer reads or returns `flex_default_range_days` / `flex_init_range_days`; `ib_client_for_api` never output them, so no HTTP answer changes. The `settings` columns stay (see the `settings` table). Affected downstreams: api — none required (old names still import); worker — none required; Flex plugin — none (0.7.0 reads Golden Source) |
| — | 0.40.0 | No DDL. **Keyset cursor for executions and cash transactions (TD-51), additive.** New `portfolio.reader.keyset` (opaque urlsafe-base64 JSON cursors, `InvalidCursor(ValueError)` for anything it did not issue) and `get_executions_page` / `get_transactions_page` (also on the monitor reader): the same filters, columns and order as `get_executions` / `get_transactions`, plus `cursor` in and `{"items", "next_cursor"}` out (`next_cursor` null on the last page; `limit + 1` is read to know). Executions key: `trade_date DESC NULLS LAST, exec_time DESC NULLS LAST, account_executions_id DESC` (the id is unique per view, so the key is total); the predicate treats a NULL cursor value as the last segment of its column and carries `exec_time` exactly (ISO with microseconds; the row's `time` epoch float cannot be compared). Transactions key: `ts DESC, account_transactions_id DESC`. **One order change:** `get_transactions` breaks `ts` ties by `account_transactions_id DESC` (before, Postgres returned tied rows in any order) and orders by the stored `ts`, not its epoch alias, so postgres_fdw now ships ORDER BY and LIMIT to Golden Source instead of pulling the whole table. Rows returned by `get_executions` / `get_transactions` are otherwise unchanged. Affected downstreams: **api** (0.6.9: `cursor` / `next_cursor` on GET /executions and /transactions, floor `bifrost-core>=0.40.0`), frontend / worker / Research (none) |
| — | 0.40.1 | No DDL, no behaviour change. The execution view builders (`_EXEC_CANONICAL_COLS`, `_env_attributed`, `_create_brokerage_views`) moved to `persistence/postgres/brokerage_views.py`; `brokerage_ddl` re-exports them (code-health: files over 800 lines back to 4; generated SQL byte-identical). Tests: the IB Gateway Redis key list is `tests/contracts/redis_ib_keys.json`, shared byte-for-byte with bifrost-platform-plugin (TD-31); `scripts/test_db.sh --sidecar` runs the db tests against a CI postgres sidecar, accepted only on loopback with the `bifrost.throwaway=on` marker (TD-48). Affected downstreams: none |
| Wave 14 | 0.41.0 | **DDL (TD-43 / TD-56 / TD-71), `migrate_wave14_trade_invariants()` in [`wave14_migrations.py`](../src/bifrost_core/persistence/postgres/wave14_migrations.py), run by `_ensure_tables`; idempotent, catalog-guarded (a non-owner on a converged DB changes nothing).** `strategy_plan.strategy_instance_id` FK SET NULL → **RESTRICT**; CHECK `strategy_plan_filled_instance_ck` `(status = 'filled') = (strategy_instance_id IS NOT NULL)`; `trade_review.strategy_instance_id` FK CASCADE → **RESTRICT**; `preference_position_category_tags.category_id` and `watchlist.category_id` int4 → **int8**; UNIQUE `preference_position_categories_name_uq (name)`; CHECK `strategy_opportunity_scope_type_ck` (NULL · watchlist_stk · explicit_symbols). A CHECK is added NOT VALID then validated — rows that break it leave it NOT VALID with a WARNING and the next refresh retries; the UNIQUE is skipped with a WARNING while a name is duplicated. 0 violating rows on DEV / STG / PROD (read 2026-10-03). The 3 orphan `Option Pool` symbol-order rows per env are deleted by an Owner step (infra `scripts/release/db-steps.d/`), not here. `strategy_plan.filled_at` is no longer written (reads join the instance's `opened_at`); the column is dropped next wave. Code: `instance_state` (derived `state` / `closed_on` on `list_instances`); `delete_instance_strict` refuses while a plan was filled by the instance or it has a review (409); category rename / delete carry `preference_market_streams_symbol_order` in the same transaction, a taken name is 409 (WriteConflict), `Uncategorized` reserved (400); `scope_type` validated (Literal `ScopeType`, `normalize_scope_type`), `watchlist_stk` needs ≥ 1 symbol; legs: `AbstractLeg` (= `TemplateLeg` = `StructureLeg`) validates structure writes, `abstract_leg_to_plan_leg` (TD-44, no rename). Affected downstreams: **api** 0.6.12 (`state` / `closed_on` on `InstanceRow`; category rows drop `id`), **frontend** (open / closed from `state`), **research** MCP `trade.strategy.instances` (reads `state`) |
| — | 0.42.0 | No DDL. **Naming program R0 + R1 (decision pack 2026-10-03, D1–D11).** **R0, public response change (D9, TD-19):** the old keys go — `trade_count` from the performance summary, every `realized_by_*` row and the calendar rows (`fill_count` stays), `calendar_by_sec_type` OPT pair rows keep only `pair_count`, and the win-rate rows lose `total_instances` (`total_trades` stays). The writers' user-facing reasons say trade and fill instead of strategy instance / execution (`No trade 41.`, `3 fills are attributed to this trade.`, `This fill is split across 2 trades; send fill_splits: [] …`). **R1, additive:** new `monitor.reader.trade_names`; every reader row that carries an instance key now carries the trade name beside it with the same value — `trade_id` (= `strategy_instance_id`), `trade_label`, `trade_opened_at_epoch`, `fill_splits: [{trade_id, quantity, strategy_opportunity_id?, trade_label?}]` beside `instance_allocations`, `realized_by_trade` beside `realized_by_strategy_instance`, and on `trade_review` rows `tags_added_json` / `tags_dropped_json` beside `tags_added` / `tags_dropped`; strict deletes answer `trade_id` too. Writers (`insert_one_execution`, `update_one_execution`, `patch_execution`, `patch_review`, `PlanLinkFillBody`) take the new names as well; when both are sent the new one wins. IB's TradeID is not read by any Trade-side SQL, so `trade_id` in a reader row is always the Trade. New `monitor.reader.data_probe` + `StatusReader.get_data_probe()` (D8-A): activity per source, a sample count and the selective-clone groups (seed + FK closure from `pg_constraint`) so the Ops platform stops naming Trade tables. The tables keep their names until R3. Affected downstreams: **api** 0.7.0 (new routes and names, floor `bifrost-core>=0.42.0`); frontend — none required (it reads `fill_count` / `total_trades` since fe a25c2e8d); worker / Research / platform — none |
| Wave 15 | 0.43.0 | **Column drops (TD-43 option B step 3/4, TD-73 option A; Owner-approved 2026-10-03).** `strategy_plan.filled_at`, `strategy_instance.notes` and `trade_review.note` leave `_ensure_tables` (a fresh database never has them) and core stops naming them: `filled_at` stays in plan reads as the instance's `opened_at` (unchanged since 0.41.0); `create_instance` / `update_instance` / `StatusReader.create_strategy_instance` / `update_strategy_instance` lose the `notes` parameter, `INSTANCE_PATCHABLE` loses `notes`, `REVIEW_PATCHABLE` and `_COLUMNS` lose `note`; `patch_instance` / `patch_review` / `save_review` refuse a `notes` / `note` key (WriteInvalid, naming the Research journal) rather than drop it; `StrategyInstanceCreateBody` / `StrategyInstanceUpdateBody` / `TradeReviewBody` lose the fields. Works with the columns present or absent, so the release goes first; **the DROP is not in db-init** — it is an Owner db-step run after the deliver (infra `scripts/release/db-steps.d/2026-10-03-td43-td73-drop-columns.md`: one transaction, refuses while any value is non-null; 0 non-null on DEV / STG / PROD, read 2026-10-03). Nothing in db-init adds them back (tested). Rollback after the drop: `ADD COLUMN` (and backfill `filled_at` from the instance's `opened_at` for filled plans), only needed below core 0.41.0. Affected downstreams: **api** 0.7.1 (floor `bifrost-core>=0.43.0`: `notes` / `note` leave the request bodies and `TradeRow`); frontend — none (reads `filled_at` from the API, sends no `notes` / `note`); worker / Research / platform — none |
| — | 0.44.0 | No DDL. **Data probe publishes the optionable watchlist (Owner 2026-10-03, option A), additive.** `read_data_probe` / `StatusReader.get_data_probe()` gain `watchlist: {label: "optionable_stocks", symbols, count}` — `upper(trim(symbol))` of `watchlist` rows with `sec_type = 'STK' AND optionable AND trim(symbol) <> ''`, distinct and sorted; a missing table is `symbols: null, count: null, detail: "missing"`, never an empty list. The Ops platform's `GET /api/v1/watchlist/union` reads it over HTTP instead of selecting from `public.watchlist` by pod exec. Affected downstreams: **api** 0.7.2 (floor `bifrost-core>=0.44.0`; the route passes the reader's dict through); platform reads the new key; frontend / worker / Research — none |
| Naming R3 | 0.45.0 | **DDL by an Owner step, not db-init (naming program R3; decision pack 2026-10-03 D1-A, D2-A, D7-A; Owner-approved).** Renames, one transaction per env ([`rename_trade_entity.py`](../src/bifrost_core/persistence/postgres/rename_trade_entity.py), `scripts/db/rename_trade_entity.py --env dev|stg|prod [--commit]`, ROLLBACK unless `--commit`; infra `scripts/release/db-steps.d/2026-10-04-r3-rename-trade-entity.md`): `strategy_instance` → **`trade`** (`strategy_instance_id` → `trade_id`; sequence, `trade_pkey`, `trade_id_account_uq`, `trade_strategy_opportunity_id_fkey`, indexes `trade_opportunity_id` / `trade_account_opened`); `strategy_instance_execution` → **`trade_execution`** (`trade_execution_id`, `trade_id`, `allocated_quantity` → **`split_quantity`**; sequence, `trade_execution_pkey` / `_trade_fk` / `_uq` / `_qty_ck`, indexes `trade_execution_whole_uq` / `_trade_ix`); `strategy_plan.strategy_instance_id` → `trade_id` (`strategy_plan_trade_id_fkey`, index `strategy_plan_trade`; the CHECK keeps its name `strategy_plan_filled_instance_ck`); `trade_review.strategy_instance_id` → `trade_id` (`trade_review_trade_id_fkey` / `_key`), `tags_added` / `tags_dropped` → `tags_added_json` / `tags_dropped_json`. No data is rewritten. **Env views:** `brokerage.executions` / `_final` / `_fly` / `_tws` output `trade_id` (the Trade), `ib_trade_id` / `ib_related_trade_id` (IB's TradeID / RelatedTradeID; Golden Source's own views keep the vendor names) and, one version, `strategy_instance_id` (= `trade_id`); new view **`brokerage.trade_fill_splits`** (`account_id, account_executions_id, trade_id, quantity, exec_id`). **Compatibility objects, one version (R4 drops them):** views `public.strategy_instance` (no `notes`), `public.strategy_instance_execution` (old column names, auto-updatable — core 0.44.0's whole-fill upsert `ON CONFLICT … WHERE allocated_quantity IS NULL` works through it) and `brokerage.instance_allocations` (over `trade_fill_splits`). Code: every SQL statement uses the new names and aliases them back to the row keys the API serves (unchanged: `strategy_instance_id` beside `trade_id` until R4); new [`trade_ddl.py`](../src/bifrost_core/persistence/postgres/trade_ddl.py) (the entity's DDL, moved out of `ddl.py`); `brokerage_tables.TRADE_EXECUTION` / `TRADE_FILL_SPLITS` (old `INSTANCE_EXECUTION` / `INSTANCE_ALLOCATION` alias them one version; `COMPAT_INSTANCE_ALLOCATIONS` names the compatibility view); `trade_ddl.TRADE_EXECUTION_DDL` (`STRATEGY_INSTANCE_EXECUTION_DDL` alias); wave 14 names the renamed constraints; `data_probe` picks the trade table from candidates (`trade`, then `strategy_instance`) and only a real table — never the compatibility view — seeds a clone group. **`_ensure_tables` refuses** (RuntimeError before its first change) while `public.strategy_instance` is a base table; `setup_fdw_foreign_tables` checks `public.trade_execution`. Retired: `td09_attribution` and `scripts/db/td09_migrate_attribution.py` (ran on dev / stg / prod 2026-10-03; they named the old tables). Reverse: `--reverse` (`rename_trade_entity_reverse.py`, embeds core 0.44.0's env view SQL) — run it **before** going back to core 0.44.0, whose db-init fails on the compatibility views. Rehearsed on DEV's schema (Postgres 16 and 17, DEV and STG/PROD view owners): dry run changes nothing; commit keeps every count; 0.45.0 db-init is a no-op after it; reverse restores an identical `pg_dump --schema-only`. Affected downstreams: **api** 0.7.3 (floor `bifrost-core>=0.45.0`; no SQL of its own); **platform** (reads only `/api/ops/data-probe`: `clone_groups[trades]` now seeds `trade`); worker / Flex / Research / frontend — none (they do not name these tables) |
| — | 0.46.0 | No DDL, no Redis key, no HTTP change. **TD-80 C1-b (Owner 2026-10-04, option C) + TD-75 aliases, public Python names removed — every one had no caller left in api 0.8.1, worker 0.2.5, Flex 0.8.1, Research, the platform repos or the plugins (re-checked on origin/main 2026-10-04).** Alias modules deleted: `config.startup` (use `config.yaml_config`), `monitor.redis_url` (use `core.redis_url`), `monitor.integrations.daemon_ib_edge` (use `monitor.integrations.platform_ib_gateway`). `monitor.reader` keeps only `StatusReader` and the outcome classes (`ReadFailed`, `WriteError`, `WriteNotFound`, `WriteConflict`, `WriteInvalid`, `WriteFailed`) at package level: the re-exports of `portfolio.reader.accounts`' `insert_one_execution` / `update_one_execution` / `delete_one_execution` / `update_execution_commission` / `write_account_executions_to_db` / `upsert_account_transactions` / `sync_accounts_snapshot_to_db` / `batch_update_execution_strategy`, of `status.write_control_command` / `write_run_status` / `write_heartbeat_interval` and of `settings.write_ib_config` are gone, and so is the fallback that imported `reader.<submodule>` on attribute access (import the submodule). `StatusReader` loses 23 members with no caller: `close`, `add_watchlist`, `delete_watchlist`, `update_strategy_instance`, `delete_strategy_instance`, `update_position_category`, `delete_position_category`, `delete_instrument_class`, `get_bar_times_in_range`, `get_executions_by_contract_keys`, `list_dims_for_type`, `get_gates_by_id`, `get_executions`, `get_executions_with_opt_pairs_single_query`, `get_net_cash_flow`, `get_transactions`, `get_executions_for_strategy_link`, `batch_update_execution_strategy`, `get_instance_open_option_legs`, `get_bars_latest`, `get_is_us_trading_day`, `get_bars_coverage`, `get_operations` (the module functions `gate_safety.get_gates_by_id` and `executions.get_executions` / `get_executions_with_opt_pairs_single_query` / `get_net_cash_flow` / `get_transactions` stay — worker and core itself call them). Module functions left without a caller, deleted: `watchlist.add_watchlist` / `delete_watchlist`, `strategy_instance.update_instance` / `delete_instance` / `get_instance_open_option_legs`, `position_categories.update_position_category` / `delete_position_category`, `instrument_class.delete_instrument_class`, `market.get_bar_times_in_range` / `get_bars_latest` / `get_bars_coverage` / `get_is_us_trading_day` / `get_is_us_trading_day_conn`, `executions.get_executions_by_contract_keys` / `get_executions_for_strategy_link`, `strategy_dim_catalog.list_dims_by_type`, `accounts.batch_update_execution_strategy`, `status.get_operations` / `write_heartbeat_interval`, `strategy_structure_write.delete_structure_strict`, `market_read_client.get_bars_latest_via_plugin` / `get_bar_times_in_range_via_plugin` / `get_bars_coverage_via_plugin`, and the whole `monitor.services.market_jobs` module (its api routes went in api 0.8.0). TD-75: `redis_health_keys.BIFROST_HEALTH_DAEMON_TRADING_ENGINE` and `postgres_sink.PostgreSQLSink` (one-version aliases since 0.39.0) are gone; `LEGACY_BIFROST_HEALTH_DAEMON_TRADING_ENGINE` / `LEGACY_BIFROST_OPS_TRADING_ENGINE_META` stay (api normalises with them). Affected downstreams: none at their current versions (api ≥ 0.8.1, worker ≥ 0.2.5 and Flex ≥ 0.8.1 import the canonical paths; their `tests/test_core_alias_imports.py` refuse the old ones). A Flex image must not be built from a checkout older than 0.8.1 against this core |
| Naming R4 | 0.47.0 | **Public interface change; DDL by an Owner step (naming program R4; decision pack 2026-10-03 D4-A / D7-A; TD-80 C3).** **Reader rows carry only the trade names**: `trade_id`, `trade_label`, `trade_opened_at_epoch`, `fill_splits: [{trade_id, quantity, strategy_opportunity_id, trade_label?}]`, `realized_by_trade`, and on `trade_review` rows `trade_id` / `tags_added_json` / `tags_dropped_json` — the R1 keys beside them (`strategy_instance_id`, `strategy_instance_label`, `strategy_instance_opened_at_epoch`, `instance_allocations` with `allocated_quantity`, `realized_by_strategy_instance`, `tags_added` / `tags_dropped`) are gone, and strict deletes answer `{deleted, trade_id}`. **Writers take only the new names**: `patch_execution` refuses `strategy_instance_id` / `instance_allocations` (WriteInvalid, unknown key), `insert_one_execution` / `update_one_execution` ignore them, `patch_review` refuses `tags_added` / `tags_dropped`, `PlanLinkFillBody` reads `trade_id` only; keyword arguments `strategy_instance_id(s)` → `trade_id(s)` on the readers (`list_instances`, `get_executions*`, `get_performance_stats`, …). `monitor.reader.trade_names` is removed; `executions.attach_instance_allocations` → `attach_fill_splits`, `weight_realized_for_strategy_instance` → `weight_realized_for_trade`, `accounts.replace_execution_instance_allocations` → `replace_execution_fill_splits`. **Facade (TD-80 C3):** `StatusReader.list_trades` / `get_trade_by_id` / `create_trade` / `get_trade_win_rate` / `get_performance_trade_summary` / `get_position_trade_attribution`; the instance-era method names stay one version as aliases. **R3 aliases removed:** `brokerage_tables.INSTANCE_EXECUTION` / `INSTANCE_ALLOCATION` / `COMPAT_INSTANCE_ALLOCATIONS` / `LEGACY_INSTANCE_ALLOCATION`, `trade_ddl.STRATEGY_INSTANCE_EXECUTION_DDL`; `data_probe.TRADE_TABLES` is `("trade",)`. **DDL in code:** the env views lose `strategy_instance_id` and their rebuild drops `brokerage.instance_allocations` by name (`brokerage_views.RETIRED_ENV_VIEWS`) — but db-init's FDW step never reaches the rebuild in dev / stg / prod (`must be owner of foreign server`, logged as `FDW setup skipped`), so the R4 step rebuilds them; `ensure_trade_tables` no longer creates `account_execution_instance_allocation`. **DDL, Owner step after the env runs 0.47.0** ([`drop_trade_compat.py`](../src/bifrost_core/persistence/postgres/drop_trade_compat.py), `scripts/db/drop_trade_compat.py --env dev|stg|prod [--commit] [--reverse]`; infra `scripts/release/db-steps.d/2026-10-08-r4-drop-compat.md`): one transaction per env, guards first (database; the compatibility objects are views; every object dropped or rebuilt owned by `bifrost`; no other view depends on them — the rebuild uses CASCADE; the frozen table holds 2 rows and each is in `trade_execution`), then `DROP VIEW public.strategy_instance_execution` / `public.strategy_instance`, the five env views rebuilt as 0.47.0 builds them (dropping `brokerage.instance_allocations`) and `GRANT SELECT` on them to the env's runtime role `trade_app_<env>` (TD-85; the DROP takes their grants with it — bifrost's default privileges would give it back too), and `DROP TABLE account_execution_instance_allocation` after a CSV export; a report RAISEs on a changed count, a left object, a view with `strategy_instance_id` or a rebuilt view `trade_app_<env>` cannot read. `--reverse` puts the objects back (R3 / 0.45.0 / 0.46.x definitions, the table empty; rows from the CSV). Retired: `rename_trade_entity` / `rename_trade_entity_reverse` and `scripts/db/rename_trade_entity.py` (ran on dev / stg / prod 2026-10-04; the SQL stays in infra). Affected downstreams: **api** 0.9.0 (floor `bifrost-core>=0.47.0`; deletes the replaced routes and old names in the same round); worker / Flex — none (they call none of these); Research / frontend / platform — none in core (their old-name reads go in the same round) |
| — | 0.47.0 | **TD-74: `settings.flex_default_range_days` / `flex_init_range_days` leave the DDL; the drop is an Owner step (infra `scripts/release/db-steps.d/2026-10-10-td74-drop-settings-flex-columns`, when: after; observation gate waived by the Owner 2026-10-04).** `CREATE TABLE settings` no longer declares them and `wave13_migrations` no longer sets them NOT NULL, so a fresh database never has them and db-init never adds them back; core has not read them since 0.39.0 (the Flex Query plugin keeps the range in Golden Source `ops_jobs.flex_settings` since Flex 0.7.0). Core works with the columns present or absent, so it ships before the drop. No reader output named them. |
| — | 0.47.0 | No DDL, no Redis key. **TD-80 C2-a (Owner 2026-10-04, option C, item 4), additive:** the Write* twins of the POST / PUT writers the `StatusReader` facade still carried — `strategy_instance.create_instance_strict` (returns the row as `get_instance_by_id` reads it; an opportunity that does not exist is `WriteInvalid`), `position_categories.create_position_category_strict` (the row; reserved `Uncategorized` `WriteInvalid`, a name in use `WriteConflict`), `position_categories.set_position_category_tag_strict` (`{account_id, contract_key, category_id, cleared}`; a category that does not exist is `WriteInvalid`; clearing an untagged position is not an error), `position_categories.set_market_streams_symbol_order_strict` (`{category_name, symbols}`; a blank, non-text or repeated symbol is `WriteInvalid` and nothing changes) and `instrument_class.set_instrument_class_strict` (full replace, the row). Each takes a status config or a connection (`write_support.write_connection`), runs in one transaction and raises `WriteInvalid` / `WriteConflict` / `WriteFailed` (`unavailable` when Postgres is not configured or unreachable); blank text is refused, never stored as NULL (as PATCH). The bool / `(id, error)` writers and the five facade write methods (`create_strategy_instance`, `create_position_category`, `set_position_category_tag`, `set_market_streams_symbol_order`, `set_instrument_class`) stay this release; C2-b deletes them once api ≥ 0.9.0 is live everywhere. Affected downstreams: **api** 0.9.0 (floor `bifrost-core>=0.47.0`) calls the new writers from `POST /trades`, `POST /position-categories`, `PUT /position-categories/tag`, `PUT /position-categories/symbol-order` and `PUT /instrument-classes/{contract_key}`; worker / Flex / Research — none |
| W4 | 0.48.0 | **DDL, additive (db-init): `position_snapshot_daily` and `account_nav_daily`** ([`snapshot_ddl.py`](../src/bifrost_core/persistence/postgres/snapshot_ddl.py), `_ensure_tables`; Owner-approved 2026-09-30 / 2026-10-05, PROD DDL list `W4-prod-ddl-plan.md`). New module `portfolio.snapshot` (`capture`, `enrich`, `split_rows`, `vendor_option_ticker`; `python -m bifrost_core.portfolio.snapshot`). No existing table, view, grant or public function changes. Affected downstreams: **api** — none in code; its image carries the job (floor `bifrost-core>=0.48.0` for the CronJob's image only); **infra** — the per-env CronJob `position-snapshot-daily`; worker / Flex / Research / frontend — none (Performance may read `account_nav_daily` later) |
| — | 0.48.1 | No DDL. `portfolio.snapshot.enrich` reads the market-data plugin's `/stocks/db/bars/benchmark` `bar_time` as epoch seconds of the bar date (it compared it as an ISO string, so 0.48.0 never filled `underlying_close` or a stock row's `mark`); a `close` of 0 counts as none. Affected downstreams: none (the api image carries the CronJob) |

## Brokerage tables

Per-env FDW name; the physical table is the same name under Golden Source `raw_broker.*`. Columns and the
non-vendor columns (`strategy_*`, `legacy_account_executions_id`, `id` PKs) are in
[BROKERAGE_GOLDEN_SOURCE.md](BROKERAGE_GOLDEN_SOURCE.md).

`legacy_account_executions_id` on the three `executions_raw_*` tables is a **historical map column**: it holds, for
rows that predate the split, their id in the single pre-split `account_executions` table. Nothing writes it (new rows
take the NULL default; the explicit NULL in the manual-execution insert was dropped in core 0.32.0) and nothing reads
it. It is kept, not dropped, because the values are the only map back to the old ids.

| Per-env FDW (`raw_broker.*` in Golden Source) | Legacy public name |
|-----------------------------------------------|--------------------|
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

Bridge tables remain per-env:

- `trade_execution` (core 0.37.0 as `strategy_instance_execution`, renamed in 0.45.0) — see
  [above](#trade_execution-core-0370-as-strategy_instance_execution-renamed-in-0450); keyed by the fill
  (`account_id`, `exec_id`), not by a view id
- `account_execution_instance_allocation` — the splits before TD-09, keyed by the unified `account_executions_id`;
  frozen from core 0.37.0, not created from 0.47.0, dropped by the Owner's naming R4 step after a CSV export (D7-A)
- `account_execution_option_stock_link` — links an option execution to the stock fill(s) of its exercise or
  assignment (`role` ∈ exercise · assignment); no FK at all

`_ensure_tables()` does **not** recreate migrated brokerage objects in `public`.
`option_trades` is P7-retired (Market Data Plugin) and is also not created.

## Market FDW tables (core 0.8.3)

| Per-env FDW | Golden Source | Purpose |
|-------------|---------------|---------|
| `market.ticker` | `raw_market.ticker` | Full ticker catalog (FDW foreign table) |
| `market.us_market_holiday` | `raw_market.us_market_holiday` | Exchange holiday / early-close calendar (FDW foreign table) |
| `market.ticker_related` | `raw_market.ticker_related` | Related tickers per symbol, ranked (FDW foreign table) |
| `market.v_us_equity_universe` | — (local view over `market.ticker`) | Local VIEW: active US CS equities |
| `public.v_us_equity_universe` | — | **Stable Trade read contract** over `market.v_us_equity_universe` (adds synthetic `tickers_id`); not scheduled for removal |

Setup: `setup_fdw_market_tables()` in [`brokerage_ddl.py`](../src/bifrost_core/persistence/postgres/brokerage_ddl.py) imports
the three tables listed in `MARKET_FOREIGN_TABLES` ([`market_tables.py`](../src/bifrost_core/persistence/postgres/market_tables.py)).
Requires `golden_source_server` to exist (created by `setup_fdw_foreign_tables`). The Market Data Plugin writes
the Golden Source side; Trade only reads.

#### `market.us_market_holiday`

Replaces the retired `public.reference_us_holidays`. Read by core
[`monitor/reader/market.py`](../src/bifrost_core/monitor/reader/market.py) (`get_market_holidays_conn`) and by trade-api `GET /market/holidays`. (The trading-day check
`get_is_us_trading_day_conn` left in core 0.46.0 with its route.)
`POST` / `DELETE /market/holidays` answer 405 — the calendar is the Plugin's.

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `exchange` | text | no | e.g. `NYSE` |
| `holiday_date` | date | no | |
| `name` | text | yes | |
| `status` | text | yes | `closed` (vendor `closed`/`holiday`) or `early-close`; only `closed` makes a weekday a non-trading day |
| `open_time` / `close_time` | timestamptz | yes | the vendor's `open` / `close` for that date (session bounds on an early close) |
| `fetched_at` | timestamptz | no | when the Plugin pulled the row |

#### `market.ticker_related`

Replaces the retired `public.ticker_related_tickers`. Read by trade-api
`GET /research/data/ticker-overview/{symbol}` (top 12 `to_symbol` by `rank` for `from_symbol`).

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `from_symbol` | text | no | the symbol asked about |
| `to_symbol` | text | no | a related symbol |
| `rank` | int4 | no | ascending = more related |
| `fetched_at` | timestamptz | no | when the Plugin pulled the row |

Retired (core 0.8.3): `public.us_equity_universe` (physical table), `public.sepa_symbol_price_readiness` (physical table), `public.v_sepa_us_equity_universe` (view), `public.v_sepa_symbol_price_readiness` (view), `universe_sync.py` (Plugin API sync module). Universe data now comes directly from Golden Source via FDW. Price readiness summary is computed at query time from Plugin API `/readiness/bar-aggregate`.

Retired (core **0.10.10** / Market Data Plugin **0.7.9**): `public.preference_data_gap_ack` (and legacy `preference_sepa_gap_ack`) — source-void acknowledgments now live in Golden Source `ops_jobs.data_source_void` via Plugin `/market/readiness/source-void`. Trade `/research/data/readiness/*` is a thin HTTP passthrough for readiness summary / gap-ack / backfill enqueue.

Retired (core **0.15.0** / Wave 6): `public.ops_audit_log` — actuation audit routed to platform-api `POST /api/v1/audit/append`; table dropped idempotently on `_ensure_tables()`.

## Feedback tables (Golden Source, trade-api owned — D-Journal-Stores 2026-09-27)

`ops_feedback.*` lives in `bifrost_golden_source` (installation-keyed: one report
stream across dev/stg/prod), written and read by **trade-api** over its
Golden Source connection as `analytics_writer` — the `ops_jobs.*` precedent for non-Research schemas
in Golden Source. Research never writes this schema; Trade never writes
`journal.*` (the mirror rule, spine `D-Journal-Stores`).

| Table | PK | Purpose |
|-------|----|---------|
| `ops_feedback.report` | `report_id` (identity; shown as `FB-%04d`) | One feedback report: `kind` bug·data·idea·howto, `title`/`body_md`, page route+label, `blocks_trading` (reporter-set at submit), `context` jsonb (shell-collected), `status` new·triaged·progress·fixed·answered·wontfix, `reply_md`/`replied_at`, `unread_reply` (a reply or status move sets it; the My-reports pane on screen clears it) |
| `ops_feedback.report_image` | `report_image_id` | ≤4 images per report (design cap), `bytes` bytea ≤2 MB each, `ON DELETE CASCADE` |

DDL is idempotent and runs in trade-api's db-init (`bifrost_api.research.feedback_schema`,
called by `scripts/run_db_refresh_schema.py`, api 0.6.8, TD-77) — never on a request; a
missing schema answers 503. Objects are owned by `bifrost` (the db-init role); changes
are additive only, since all three environments share the schema. Behaviour contracts:
design `Shell Spec §20`; API under trade-api `/research/feedback/*`.

## Commands

```bash
make db-init                 # per-env DDL + brokerage schema + FDW (if golden_source configured)
make db-init-brokerage       # Golden Source brokerage DDL only
make db-init-brokerage-fdw   # + FDW into current per-env DB (needs superuser)
```

See also [BROKERAGE_GOLDEN_SOURCE.md](BROKERAGE_GOLDEN_SOURCE.md), [DAEMON_IPC_REDIS.md](DAEMON_IPC_REDIS.md), and [GOLDEN_SOURCE_RETENTION.md](../../bifrost-trade-infra/docs/GOLDEN_SOURCE_RETENTION.md).

## Appendix — public columns (bifrost_dev, 2026-10-01)

Generated from DEV `information_schema.columns` + `pg_constraint` on 2026-10-01 — the 18 base tables in `public`;
`trade`, `strategy_plan.trade_id` and `trade_review` are shown under their naming R3 names (core 0.45.0), and
`trade_execution` (TD-09, after this snapshot) is documented in the section above.
STG / PROD run the same `_ensure_tables()`; when a table here disagrees with `ddl.py`, the live DB wins and
this appendix is stale — regenerate it. Meaning of the jsonb columns and the state machines is in the sections
above; this is the type-level reference. "Unified execution id" = `brokerage.executions.account_executions_id`:
Flex `executions_raw_flex_id` (> 0), TWS `-executions_raw_tws_id`, journal `-(1000000000 + executions_raw_journal_id)`.

Enum types (labels, unordered): `dim_direction_t` bearish · bullish · neutral; `dim_structure_t` butterfly ·
calendar · condor · custom · diagonal · ratio · single_leg · straddle · vertical; `dim_coverage_t`
cash_secured · covered · naked · synthetic; `dim_risk_t` defined · undefined; `dim_volatility_t` long_vol ·
short_vol · vol_neutral; `dim_time_t` flex · leaps · monthly · weekly.

The one public view, `v_us_equity_universe`, is `market.v_us_equity_universe` (`symbol`, `name`, `market`,
`locale`, `primary_exchange`, `instrument_type`, `active`, `sector`, `industry`, `list_date`, `market_cap`) plus
`tickers_id = hashtext(upper(trim(symbol)))::bigint`.

#### `account_execution_instance_allocation` (dropped by the naming R4 step; core 0.47.0 no longer creates it)

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `account_execution_instance_allocation_id` | int8 | no | bigserial; PK |
| `account_id` | text | no |  |
| `account_executions_id` | int8 | no | UNIQUE (account_executions_id, strategy_instance_id); unified id of `brokerage.executions` (see below); no FK — the execution lives in Golden Source |
| `strategy_instance_id` | int8 | no | UNIQUE (account_executions_id, strategy_instance_id); FK → `trade.trade_id` ON DELETE RESTRICT (column name kept: the table is frozen and goes in R4) |
| `allocated_quantity` | float8 | no | signed share of the fill; the rows of one execution must sum to its signed quantity (checked in core, not by the DB) |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |

#### `account_execution_option_stock_link`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `account_execution_option_stock_link_id` | int8 | no | bigserial; PK |
| `account_id` | text | no |  |
| `option_account_executions_id` | int8 | no | UNIQUE (option_account_executions_id, stock_account_executions_id); unified execution id of the option leg; no FK |
| `stock_account_executions_id` | int8 | no | UNIQUE (option_account_executions_id, stock_account_executions_id); unified execution id of the stock fill; no FK |
| `role` | text | yes | CHECK ∈ exercise · assignment |
| `note` | text | yes |  |
| `created_at` | timestamptz | no | `now()` |

#### `gate_safety_strategy`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `gate_safety_strategy_id` | int8 | no | bigserial; PK |
| `name` | text | no |  |
| `version` | int4 | no | `1` |
| `is_active` | bool | no | `true` |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |
| `dim_direction` | dim_direction_t | yes |  |
| `dim_structure` | dim_structure_t | yes |  |
| `dim_coverage` | dim_coverage_t | yes |  |
| `dim_risk` | dim_risk_t | yes |  |
| `dim_volatility` | dim_volatility_t | yes |  |
| `dim_time` | dim_time_t | yes |  |
| `params_json` | jsonb | no | `'{}'` |

#### `preference_instrument_class`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `preference_instrument_class_id` | int8 | no | bigserial; PK |
| `contract_key` | text | no | UNIQUE |
| `instrument_class` | text | no | CHECK ∈ stock · fixed_income · cash_like |
| `note` | text | yes |  |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |

#### `preference_market_streams_symbol_order`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `category_name` | text | no | PK (category_name, symbol); category by name, no FK — the name is UNIQUE and core carries a rename / delete to these rows in the same transaction (0.41.0); `Uncategorized` = positions without a category (a reserved category name) |
| `symbol` | text | no | PK (category_name, symbol) |
| `sort_order` | int4 | no | `0` |
| `updated_at` | timestamptz | yes | `now()` |

#### `preference_position_categories`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `id` | int8 | no | bigserial; PK; legacy name — not `<table>_id` |
| `name` | text | no | UNIQUE `preference_position_categories_name_uq` (0.41.0); `Uncategorized` reserved by core |
| `description` | text | yes |  |
| `sort_order` | int4 | yes |  |
| `created_at` | timestamptz | yes | `now()` |
| `updated_at` | timestamptz | yes | `now()` |

#### `preference_position_category_tags`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `account_id` | text | no | PK (account_id, contract_key) |
| `contract_key` | text | no | PK (account_id, contract_key); one category per (account, contract) |
| `category_id` | int8 | no | FK → `preference_position_categories.id` ON DELETE CASCADE (int4 before 0.41.0) |
| `created_at` | timestamptz | yes | `now()` |

#### `preference_saved_search`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `preference_saved_search_id` | int8 | no | bigserial; PK |
| `owner` | text | no | `'operator'`; UNIQUE (owner, route, label) |
| `route` | text | no | UNIQUE (owner, route, label) |
| `label` | text | no | UNIQUE (owner, route, label) |
| `state_json` | jsonb | no | `'{}'` |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |

#### `settings`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `id` | int4 | no | `1`; PK; single row; `_ensure_tables()` seeds `id = 1` |
| `ib_host_account_id` | text | yes | trading (host) IB account — `/status` `ib_client.account.trading`; monitor `POST /config/ib` |
| `stream_host_account_id` | text | yes | event-stream host account — `ib_client.account.event_host` |
| `stream_secondary_account_id` | text | yes | event-stream secondary account — `ib_client.account.event_secondary` |
| `active_strategy_structure_id` | int8 | yes | FK → `strategy_structure.strategy_structure_id` ON DELETE SET NULL; monitor `POST /config/active-strategy`; the daemon loads it at start |
| `active_gate_safety_strategy_id` | int8 | yes | FK → `gate_safety_strategy.gate_safety_strategy_id` ON DELETE SET NULL; monitor `POST /config/active-strategy`; the daemon loads the gate set at start |
| `active_strategy_allocation_id` | int8 | yes | FK → `strategy_allocation.strategy_allocation_id` ON DELETE SET NULL; monitor `POST /config/active-strategy` |

`flex_default_range_days` / `flex_init_range_days` (the Flex Query range, 30 / 360) left the DDL with TD-74: the Flex Query plugin keeps the range in Golden Source `ops_jobs.flex_settings` since 0.7.0 and core has not read them since 0.39.0. They are dropped from existing databases by an Owner db-step after the deliver (infra `scripts/release/db-steps.d/2026-10-10-td74-drop-settings-flex-columns.md`), never by db-init.

#### `strategy_allocation`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `strategy_allocation_id` | int8 | no | bigserial; PK |
| `name` | text | no |  |
| `gate_safety_strategy_id` | int8 | yes | FK → `gate_safety_strategy.gate_safety_strategy_id` |
| `is_active` | bool | no | `true` |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |
| `max_positions` | int4 | yes |  |
| `max_bp_pct` | numeric | yes |  |

#### `strategy_allocation_opportunity`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `strategy_allocation_id` | int8 | no | PK (strategy_allocation_id, strategy_opportunity_id); FK → `strategy_allocation.strategy_allocation_id` ON DELETE CASCADE |
| `strategy_opportunity_id` | int8 | no | PK (strategy_allocation_id, strategy_opportunity_id); FK → `strategy_opportunity.strategy_opportunity_id` ON DELETE CASCADE |
| `sort_order` | int4 | no | `0` |

#### `trade` (was `strategy_instance`; that name was a compatibility view from R3 until the R4 step)

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `trade_id` | int8 | no | bigserial; PK (was `strategy_instance_id`) |
| `strategy_opportunity_id` | int8 | no | FK → `strategy_opportunity.strategy_opportunity_id` ON DELETE RESTRICT |
| `account_id` | text | no | IB account the instance trades in; allocation rows must match it |
| `opened_at` | timestamptz | no | when the position was opened; a filled `strategy_plan.filled_at` is this value |
| `label` | text | yes |  |
| `notes` | text | yes | dropped 2026-10-03 (TD-73); not on a fresh database |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |

#### `strategy_opportunity`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `strategy_opportunity_id` | int8 | no | bigserial; PK |
| `name` | text | no |  |
| `strategy_structure_id` | int8 | no | FK → `strategy_structure.strategy_structure_id` |
| `default_gate_safety_strategy_id` | int8 | yes | FK → `gate_safety_strategy.gate_safety_strategy_id` |
| `scope_type` | text | yes | CHECK `strategy_opportunity_scope_type_ck` ∈ watchlist_stk · explicit_symbols (0.41.0) |
| `is_active` | bool | no | `true` |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |
| `entry_conditions_json` | jsonb | no | `'[]'` |
| `symbols_json` | jsonb | no | `'[]'` |

#### `strategy_plan`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `strategy_plan_id` | int8 | no | bigserial; PK |
| `account_id` | text | no |  |
| `symbol` | text | no |  |
| `structure_label` | text | no |  |
| `strategy_structure_id` | int8 | yes | FK → `strategy_structure.strategy_structure_id` ON DELETE SET NULL |
| `strategy_opportunity_id` | int8 | yes | FK → `strategy_opportunity.strategy_opportunity_id` ON DELETE SET NULL |
| `legs_json` | jsonb | no | `'[]'` |
| `qty` | int4 | no | CHECK `qty > 0` |
| `price_effect` | text | yes | CHECK ∈ credit · debit |
| `limit_price` | numeric | yes | CHECK `limit_price >= 0` |
| `target_kind` | text | yes | CHECK set together with `target_value`; CHECK ∈ credit_pct · option_price · underlying_price |
| `target_value` | numeric | yes | CHECK set together with `target_kind` |
| `stop_kind` | text | yes | CHECK set together with `stop_value`; CHECK ∈ credit_multiple · option_price · underlying_price |
| `stop_value` | numeric | yes | CHECK set together with `stop_kind` |
| `exit_by` | date | yes |  |
| `rationale` | text | yes |  |
| `source_kind` | text | no | `'manual'`; CHECK ∈ manual · symbol · hypothesis · inbox_draft · roll |
| `source_ref` | text | yes |  |
| `source_json` | jsonb | no | `'[]'` |
| `status` | text | no | `'draft'`; CHECK ∈ draft · intended · filled · cancelled; `expired` is derived, never stored (see §strategy_plan) |
| `expires_at` | timestamptz | yes |  |
| `intended_at` | timestamptz | yes |  |
| `filled_at` | timestamptz | yes | not written since 0.41.0, not named since 0.43.0 (reads take the instance's `opened_at`); dropped after core 0.43.0 (TD-43); not on a fresh database |
| `cancelled_at` | timestamptz | yes |  |
| `trade_id` | int8 | yes | FK → `trade.trade_id` ON DELETE RESTRICT (was `strategy_instance_id`); CHECK `strategy_plan_filled_instance_ck` (set exactly when `status = 'filled'`) |
| `parent_strategy_plan_id` | int8 | yes | FK → `strategy_plan.strategy_plan_id` ON DELETE SET NULL |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |

#### `strategy_structure`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `strategy_structure_id` | int8 | no | bigserial; PK |
| `name` | text | no |  |
| `strategy_template_id` | int8 | yes | FK → `strategy_template.strategy_template_id` |
| `version` | int4 | no | `1` |
| `is_active` | bool | no | `true` |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |
| `notes` | text | yes |  |
| `meta_json` | jsonb | no | `'{}'` |
| `legs_json` | jsonb | no | `'[]'` |

#### `strategy_template`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `strategy_template_id` | int8 | no | bigserial; PK |
| `template_code` | text | no | UNIQUE |
| `display_name` | text | no |  |
| `dim_direction` | dim_direction_t | yes |  |
| `dim_structure` | dim_structure_t | yes |  |
| `dim_coverage` | dim_coverage_t | yes |  |
| `dim_risk` | dim_risk_t | yes |  |
| `dim_volatility` | dim_volatility_t | yes |  |
| `dim_time` | dim_time_t | yes |  |
| `explanation` | text | yes |  |
| `typical_use` | text | yes |  |
| `example` | text | yes |  |
| `nature` | text | yes |  |
| `sort_order` | int4 | no | `0` |
| `is_active` | bool | no | `true` |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |
| `params_json` | jsonb | no | `'[]'` |
| `characteristics_json` | jsonb | no | `'[]'` |
| `legs_json` | jsonb | no | `'[]'` |

#### `trade_review`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `trade_review_id` | int8 | no | bigserial; PK |
| `trade_id` | int8 | no | FK → `trade.trade_id` ON DELETE RESTRICT; UNIQUE (was `strategy_instance_id`) |
| `tags_added_json` | jsonb | no | `'[]'` (was `tags_added`) |
| `tags_dropped_json` | jsonb | no | `'[]'` (was `tags_dropped`) |
| `note` | text | yes | dropped 2026-10-03 (TD-73); not on a fresh database |
| `reviewed_at` | timestamptz | yes | NULL = awaiting review |
| `created_at` | timestamptz | no | `now()` |
| `updated_at` | timestamptz | no | `now()` |

#### `watchlist`

| Column | Type | Null | Default / notes |
|--------|------|------|-----------------|
| `contract_key` | text | no | PK; `SYMBOL\|STK\|\|\|` or `SYMBOL\|OPT\|YYYYMMDD\|STRIKE\|R` |
| `symbol` | text | yes |  |
| `sec_type` | text | yes |  |
| `expiry` | text | yes |  |
| `strike` | float8 | yes |  |
| `option_right` | text | yes |  |
| `display_label` | text | yes |  |
| `source` | text | yes | writer passes `'manual'` unless told otherwise |
| `created_at` | timestamptz | yes | `now()` |
| `category_id` | int8 | yes | FK → `preference_position_categories.id` ON DELETE SET NULL (int4 before 0.41.0) |
| `optionable` | bool | yes | `false`; NULL reads as false; an update without the field keeps the stored value |
