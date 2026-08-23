"""PostgreSQL DDL: current schema (CREATE TABLE IF NOT EXISTS + indexes only)."""

# IB / brokerage tables live in bifrost_golden_source.brokerage.* (see brokerage_ddl.py).
# Per-env DBs expose them via postgres_fdw. Do not recreate in public.
# Bridge tables (account_execution_instance_allocation, account_execution_option_stock_link)
# stay in per-env public — they FK strategy_instance.
_BROKERAGE_MIGRATED_TABLES = frozenset(
    {
        "daemon_open_orders",
        "account",
        "account_positions",
        "contract_quote_live",
        "account_execution_commissions",
        "account_transactions",
        "settings_ib_flex",
        "executions_raw_tws",
        "executions_raw_flex",
        "executions_raw_journal",
    }
)
_BROKERAGE_MIGRATED_VIEWS = frozenset(
    {
        "account_executions",
        "account_executions_final",
        "account_executions_fly",
    }
)
# P7: Polygon option ticks live in Market Data Plugin, not per-env public.
_P7_RETIRED_PUBLIC_TABLES = frozenset({"option_trades"})
# 1:1 child tables merged into gate_safety_strategy (scalar columns; earnings_dates stays 1:N).
_GATE_SAFETY_RETIRED_CHILD_TABLES = frozenset(
    {"gate_safety_state", "gate_safety_intent", "gate_safety_guard"}
)
_GATE_SAFETY_MERGED_COLUMN_DDL = (
    "epsilon_band integer NOT NULL DEFAULT 10",
    "threshold_hedge_shares integer NOT NULL DEFAULT 25",
    "max_delta_limit integer NOT NULL DEFAULT 500",
    "vol_window_min integer NOT NULL DEFAULT 5",
    "stale_ts_threshold_ms integer NOT NULL DEFAULT 5000",
    "wide_spread_pct double precision NOT NULL DEFAULT 0.1",
    "extreme_spread_pct double precision NOT NULL DEFAULT 0.5",
    "data_lag_threshold_ms integer NOT NULL DEFAULT 1000",
    "min_hedge_shares integer NOT NULL DEFAULT 10",
    "cooldown_seconds integer NOT NULL DEFAULT 60",
    "max_hedge_shares_per_order integer NOT NULL DEFAULT 500",
    "min_price_move_pct double precision NOT NULL DEFAULT 0.2",
    "max_daily_hedge_count integer NOT NULL DEFAULT 50",
    "max_position_shares integer NOT NULL DEFAULT 2000",
    "max_daily_loss_usd double precision NOT NULL DEFAULT 5000.0",
    "max_net_delta_shares integer NOT NULL DEFAULT 100",
    "max_spread_pct double precision NOT NULL DEFAULT 0.05",
    "paper_trade boolean NOT NULL DEFAULT true",
)


def _upgrade_gate_safety_strategy(cur) -> None:
    """Add merged state/intent/guard columns, copy from 1:1 children, drop children.

    Idempotent: ADD COLUMN IF NOT EXISTS + DROP TABLE IF EXISTS. Safe to re-run.
    """
    alters = ", ".join(f"ADD COLUMN IF NOT EXISTS {col}" for col in _GATE_SAFETY_MERGED_COLUMN_DDL)
    cur.execute(f"ALTER TABLE gate_safety_strategy {alters}")
    cur.execute("SELECT to_regclass('public.gate_safety_state')")
    if cur.fetchone()[0] is not None:
        cur.execute(
            """
            UPDATE gate_safety_strategy s SET
                epsilon_band = st.epsilon_band,
                threshold_hedge_shares = st.threshold_hedge_shares,
                max_delta_limit = st.max_delta_limit,
                vol_window_min = st.vol_window_min,
                stale_ts_threshold_ms = st.stale_ts_threshold_ms,
                wide_spread_pct = st.wide_spread_pct,
                extreme_spread_pct = st.extreme_spread_pct,
                data_lag_threshold_ms = st.data_lag_threshold_ms
            FROM gate_safety_state st
            WHERE s.gate_safety_strategy_id = st.gate_safety_strategy_id
            """
        )
    cur.execute("SELECT to_regclass('public.gate_safety_intent')")
    if cur.fetchone()[0] is not None:
        cur.execute(
            """
            UPDATE gate_safety_strategy s SET
                min_hedge_shares = i.min_hedge_shares,
                cooldown_seconds = i.cooldown_seconds,
                max_hedge_shares_per_order = i.max_hedge_shares_per_order,
                min_price_move_pct = i.min_price_move_pct
            FROM gate_safety_intent i
            WHERE s.gate_safety_strategy_id = i.gate_safety_strategy_id
            """
        )
    cur.execute("SELECT to_regclass('public.gate_safety_guard')")
    if cur.fetchone()[0] is not None:
        cur.execute(
            """
            UPDATE gate_safety_strategy s SET
                max_daily_hedge_count = g.max_daily_hedge_count,
                max_position_shares = g.max_position_shares,
                max_daily_loss_usd = g.max_daily_loss_usd,
                max_net_delta_shares = g.max_net_delta_shares,
                max_spread_pct = g.max_spread_pct,
                paper_trade = g.paper_trade
            FROM gate_safety_guard g
            WHERE s.gate_safety_strategy_id = g.gate_safety_strategy_id
            """
        )
    for name in _GATE_SAFETY_RETIRED_CHILD_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def _ensure_tables(conn, log=None, log_table=None) -> None:
    """Apply full DDL (per DATABASE.md). CREATE IF NOT EXISTS and index DDL only.
    If log is callable, it is called with a short step name before each DDL section (for progress/debug).
    If log_table is callable, it is called as log_table(table_name, purpose) before each table is created/updated.
    """

    def _log(msg: str) -> None:
        if callable(log):
            log(msg)

    def _log_table(name: str, purpose: str) -> None:
        if callable(log_table):
            log_table(name, purpose)

    try:
        conn.rollback()
    except Exception:
        pass
    with conn.cursor() as cur:
        # Daemon / Account Sync IPC (heartbeat, run_status, control, auto_status*)
        # retired → per-env Redis (bifrost_core.persistence.redis_daemon_state).
        _log("daemon_* / account_sync_* IPC tables skipped (Redis daemon state)")

        _log("settings (account/stream + flex + active strategy refs; IB host/port/client IDs in config YAML)")
        _log_table("settings", "App settings (account IDs, stream accounts, Flex, active strategy refs)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id integer PRIMARY KEY DEFAULT 1,
                ib_host_account_id text,
                stream_host_account_id text,
                stream_secondary_account_id text,
                ib_flex_host_token text,
                ib_flex_secondary_token text,
                flex_default_range_days integer NOT NULL DEFAULT 30,
                flex_init_range_days integer NOT NULL DEFAULT 360,
                active_strategy_structure_id bigint,
                active_gate_safety_strategy_id bigint,
                active_strategy_allocation_id bigint
            )
        """
        )
        cur.execute(
            """
            INSERT INTO settings (id) VALUES (1)
            ON CONFLICT (id) DO NOTHING
        """
        )
        _log(
            "account / account_positions / contract_quote_live / commissions / "
            "transactions skipped (brokerage.* Golden Source)"
        )
        # Market feed tables (stock_day/min, option_*, tickers/ticker_overview, fundamentals,
        # job_massive_backfill, etc.) are owned by bifrost-platform-plugin-market-data
        # (market.* / data_ops.*). Core DDL no longer creates those public tables.

        _log("job_ticker_reference_state (ticker_types / ticker_related → Golden Source)")
        cur.execute("DROP TABLE IF EXISTS ticker_instrument_types CASCADE")
        # ticker_types retired → market.ticker_type (Golden Source / Plugin HTTP)
        _log("ticker_types dropped (Golden Source market.ticker_type)")
        cur.execute("DROP TABLE IF EXISTS public.ticker_types CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.ticker_instrument_types CASCADE")
        # ticker_related_tickers retired → market.ticker_related (Golden Source / FDW)
        _log("ticker_related_tickers dropped (Golden Source market.ticker_related)")
        cur.execute("DROP TABLE IF EXISTS public.ticker_related_tickers CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.stock_related_tickers CASCADE")
        _log_table("job_ticker_reference_state", "Ticker reference sync cursors / status")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS job_ticker_reference_state (
                sync_kind text PRIMARY KEY,
                last_cursor text,
                status text,
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        # One-time: migrate legacy stocks / job_stock_reference_state
        # without recreating public.tickers / ticker_overview.
        cur.execute(
            """
            DO $$
            BEGIN
              IF to_regclass('public.stocks') IS NULL THEN
                RETURN;
              END IF;

              IF to_regclass('public.job_stock_reference_state') IS NOT NULL THEN
                INSERT INTO job_ticker_reference_state (sync_kind, last_cursor, status, updated_at)
                SELECT
                  CASE sync_kind WHEN 'universe_stocks' THEN 'universe_tickers' ELSE sync_kind END,
                  last_cursor,
                  status,
                  updated_at
                FROM job_stock_reference_state
                ON CONFLICT (sync_kind) DO NOTHING;
              END IF;

              DROP TABLE IF EXISTS stock_related_tickers CASCADE;
              DROP TABLE IF EXISTS stocks CASCADE;
              DROP TABLE IF EXISTS job_stock_reference_state CASCADE;
            END $$;
            """
        )

        conn.commit()
        _log("preference_position_categories, preference_position_category_tags")
        _log_table(
            "preference_position_categories",
            "Position category definitions (preference)",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS preference_position_categories (
                id bigserial PRIMARY KEY,
                name text NOT NULL,
                description text,
                sort_order integer,
                created_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        # Older installs may predate some columns; INSERT expects updated_at etc.
        cur.execute(
            "ALTER TABLE preference_position_categories ADD COLUMN IF NOT EXISTS description text"
        )
        cur.execute(
            "ALTER TABLE preference_position_categories ADD COLUMN IF NOT EXISTS sort_order integer"
        )
        cur.execute(
            "ALTER TABLE preference_position_categories ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now()"
        )
        cur.execute(
            "ALTER TABLE preference_position_categories ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()"
        )
        _log_table(
            "preference_position_category_tags",
            "Position-to-category mapping (preference)",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS preference_position_category_tags (
                account_id text NOT NULL,
                contract_key text NOT NULL,
                category_id integer NOT NULL REFERENCES preference_position_categories(id) ON DELETE CASCADE,
                created_at timestamptz DEFAULT now(),
                PRIMARY KEY (account_id, contract_key)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS preference_position_category_tags_category_id ON preference_position_category_tags (category_id)"
        )
        _log("preference_market_streams_symbol_order")
        _log_table(
            "preference_market_streams_symbol_order",
            "Market Streams symbol order per category (preference)",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS preference_market_streams_symbol_order (
                category_name text NOT NULL,
                symbol text NOT NULL,
                sort_order integer NOT NULL DEFAULT 0,
                updated_at timestamptz DEFAULT now(),
                PRIMARY KEY (category_name, symbol)
            )
            """
        )
        _log("preference_data_gap_ack")
        # Rename legacy table if it still exists under the old sepa-prefixed name
        cur.execute(
            "ALTER TABLE IF EXISTS preference_sepa_gap_ack RENAME TO preference_data_gap_ack"
        )
        _log_table(
            "preference_data_gap_ack",
            "Data gap source-void acknowledgment per data type (preference)",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS preference_data_gap_ack (
                data_type        varchar(64) PRIMARY KEY,
                is_void          boolean NOT NULL DEFAULT false,
                acked_gap_count  integer NOT NULL DEFAULT 0,
                void_reason      text,
                acked_at         timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "ALTER TABLE preference_data_gap_ack "
            "ADD COLUMN IF NOT EXISTS acked_gap_count integer NOT NULL DEFAULT 0"
        )
        conn.commit()
        # reference_us_holidays retired → market.us_market_holiday (Golden Source / FDW)
        _log("gate_safety_*, strategy_*, settings active_*")
        _log_table("gate_safety_strategy", "Safety boundary set (strategy + state + intent + guard scalars)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_safety_strategy (
                gate_safety_strategy_id bigserial PRIMARY KEY,
                name text NOT NULL,
                version integer NOT NULL DEFAULT 1,
                dim_direction text,
                dim_structure text,
                dim_coverage text,
                dim_risk text,
                dim_volatility text,
                dim_time text,
                is_active boolean NOT NULL DEFAULT true,
                min_dte integer NOT NULL,
                max_dte integer NOT NULL,
                atm_band_pct double precision NOT NULL,
                blackout_days_before integer NOT NULL,
                blackout_days_after integer NOT NULL,
                trading_hours_only boolean NOT NULL DEFAULT true,
                epsilon_band integer NOT NULL DEFAULT 10,
                threshold_hedge_shares integer NOT NULL DEFAULT 25,
                max_delta_limit integer NOT NULL DEFAULT 500,
                vol_window_min integer NOT NULL DEFAULT 5,
                stale_ts_threshold_ms integer NOT NULL DEFAULT 5000,
                wide_spread_pct double precision NOT NULL DEFAULT 0.1,
                extreme_spread_pct double precision NOT NULL DEFAULT 0.5,
                data_lag_threshold_ms integer NOT NULL DEFAULT 1000,
                min_hedge_shares integer NOT NULL DEFAULT 10,
                cooldown_seconds integer NOT NULL DEFAULT 60,
                max_hedge_shares_per_order integer NOT NULL DEFAULT 500,
                min_price_move_pct double precision NOT NULL DEFAULT 0.2,
                max_daily_hedge_count integer NOT NULL DEFAULT 50,
                max_position_shares integer NOT NULL DEFAULT 2000,
                max_daily_loss_usd double precision NOT NULL DEFAULT 5000.0,
                max_net_delta_shares integer NOT NULL DEFAULT 100,
                max_spread_pct double precision NOT NULL DEFAULT 0.05,
                paper_trade boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        _upgrade_gate_safety_strategy(cur)
        _log("gate_safety_state / intent / guard skipped (merged into gate_safety_strategy)")
        _log_table(
            "gate_safety_strategy_earnings_dates",
            "Strategy layer earnings blacklist dates",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_safety_strategy_earnings_dates (
                gate_safety_strategy_id bigint NOT NULL REFERENCES gate_safety_strategy(gate_safety_strategy_id) ON DELETE CASCADE,
                holiday_date date NOT NULL,
                PRIMARY KEY (gate_safety_strategy_id, holiday_date)
            )
            """
        )
        _log_table("strategy_dim", "Option strategy dimension enum (dim_type + code)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_dim (
                strategy_dim_id bigserial PRIMARY KEY,
                dim_type text NOT NULL,
                code text NOT NULL,
                display_label text NOT NULL,
                sort_order integer NOT NULL DEFAULT 0,
                created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (dim_type, code)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_dim_dim_type ON strategy_dim (dim_type)"
        )
        _log_table(
            "strategy_template", "Flat option structure template (six dims + legs)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_template (
                strategy_template_id bigserial PRIMARY KEY,
                template_code text NOT NULL UNIQUE,
                display_name text NOT NULL,
                dim_direction text,
                dim_structure text,
                dim_coverage text,
                dim_risk text,
                dim_volatility text,
                dim_time text,
                explanation text,
                typical_use text,
                example text,
                nature text,
                sort_order integer NOT NULL DEFAULT 0,
                is_active boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_template_sort ON strategy_template (sort_order)"
        )
        _log_table("strategy_template_leg", "Template default legs (one row per leg)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_template_leg (
                strategy_template_leg_id bigserial PRIMARY KEY,
                strategy_template_id bigint NOT NULL REFERENCES strategy_template(strategy_template_id) ON DELETE CASCADE,
                sort_order integer NOT NULL DEFAULT 0,
                role text,
                direction text,
                option_right text,
                quantity_default integer NOT NULL DEFAULT 1,
                created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (strategy_template_id, sort_order)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_template_leg_template_id ON strategy_template_leg (strategy_template_id)"
        )
        _log_table("strategy_template_param", "Template meta param definition")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_template_param (
                strategy_template_param_id bigserial PRIMARY KEY,
                strategy_template_id bigint NOT NULL REFERENCES strategy_template(strategy_template_id) ON DELETE CASCADE,
                meta_key text NOT NULL,
                display_label text,
                default_value_text text,
                param_kind text,
                sort_order integer NOT NULL DEFAULT 0,
                created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (strategy_template_id, meta_key)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_template_param_template_id ON strategy_template_param (strategy_template_id)"
        )
        _log_table("strategy_template_characteristic", "Template characteristic lines")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_template_characteristic (
                strategy_template_characteristic_id bigserial PRIMARY KEY,
                strategy_template_id bigint NOT NULL REFERENCES strategy_template(strategy_template_id) ON DELETE CASCADE,
                sort_order integer NOT NULL DEFAULT 0,
                characteristic_text text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_template_char_template_id ON strategy_template_characteristic (strategy_template_id)"
        )
        _log_table("strategy_structure", "Structure strategy")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_structure (
                strategy_structure_id bigserial PRIMARY KEY,
                name text NOT NULL,
                strategy_template_id bigint REFERENCES strategy_template(strategy_template_id),
                version integer NOT NULL DEFAULT 1,
                is_active boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                notes text
            )
            """
        )
        _log_table("strategy_structure_leg", "Structure strategy leg (one row per leg)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_structure_leg (
                strategy_structure_leg_id bigserial PRIMARY KEY,
                strategy_structure_id bigint NOT NULL REFERENCES strategy_structure(strategy_structure_id) ON DELETE CASCADE,
                sort_order integer NOT NULL DEFAULT 0,
                role text,
                direction text,
                option_right text,
                quantity integer NOT NULL DEFAULT 1,
                strike double precision,
                expiration text,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_structure_leg_structure_id ON strategy_structure_leg (strategy_structure_id)"
        )
        cur.execute("DROP TABLE IF EXISTS strategy_structure_constraint CASCADE")
        _log("strategy_structure_constraint dropped (over-designed, never used)")
        _log_table("strategy_structure_meta", "Structure strategy metadata key-value")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_structure_meta (
                strategy_structure_meta_id bigserial PRIMARY KEY,
                strategy_structure_id bigint NOT NULL REFERENCES strategy_structure(strategy_structure_id) ON DELETE CASCADE,
                meta_key text NOT NULL,
                meta_value_text text,
                created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (strategy_structure_id, meta_key)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_structure_meta_structure_id ON strategy_structure_meta (strategy_structure_id)"
        )
        _log_table("strategy_opportunity", "Opportunity strategy")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_opportunity (
                strategy_opportunity_id bigserial PRIMARY KEY,
                name text NOT NULL,
                strategy_structure_id bigint NOT NULL REFERENCES strategy_structure(strategy_structure_id),
                default_gate_safety_strategy_id bigint REFERENCES gate_safety_strategy(gate_safety_strategy_id),
                scope_type text,
                is_active boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        _log_table("strategy_opportunity_symbol", "Opportunity strategy symbols")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_opportunity_symbol (
                strategy_opportunity_symbol_id bigserial PRIMARY KEY,
                strategy_opportunity_id bigint NOT NULL REFERENCES strategy_opportunity(strategy_opportunity_id) ON DELETE CASCADE,
                symbol text NOT NULL,
                sort_order integer NOT NULL DEFAULT 0,
                UNIQUE (strategy_opportunity_id, symbol)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_opportunity_symbol_opportunity_id ON strategy_opportunity_symbol (strategy_opportunity_id)"
        )
        _log_table(
            "strategy_opportunity_entry_condition",
            "Opportunity strategy entry conditions",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_opportunity_entry_condition (
                strategy_opportunity_entry_condition_id bigserial PRIMARY KEY,
                strategy_opportunity_id bigint NOT NULL REFERENCES strategy_opportunity(strategy_opportunity_id) ON DELETE CASCADE,
                condition_type text NOT NULL,
                value_text text,
                value_numeric double precision,
                sort_order integer NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_opportunity_entry_condition_opportunity_id ON strategy_opportunity_entry_condition (strategy_opportunity_id)"
        )
        _log_table(
            "strategy_instance", "Strategy instance (one open per opportunity/account)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_instance (
                strategy_instance_id bigserial PRIMARY KEY,
                strategy_opportunity_id bigint NOT NULL REFERENCES strategy_opportunity(strategy_opportunity_id) ON DELETE RESTRICT,
                account_id text NOT NULL,
                opened_at timestamptz NOT NULL,
                label text,
                notes text,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_instance_opportunity_id ON strategy_instance (strategy_opportunity_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_instance_account_opened ON strategy_instance (account_id, opened_at)"
        )
        _log_table("strategy_allocation", "Strategy allocation")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_allocation (
                strategy_allocation_id bigserial PRIMARY KEY,
                name text NOT NULL,
                gate_safety_strategy_id bigint REFERENCES gate_safety_strategy(gate_safety_strategy_id),
                max_positions integer,
                max_bp_pct numeric,
                is_active boolean NOT NULL DEFAULT true,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        _log_table("strategy_allocation_opportunity", "Allocation-opportunity junction")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_allocation_opportunity (
                strategy_allocation_id bigint NOT NULL REFERENCES strategy_allocation(strategy_allocation_id) ON DELETE CASCADE,
                strategy_opportunity_id bigint NOT NULL REFERENCES strategy_opportunity(strategy_opportunity_id) ON DELETE CASCADE,
                sort_order integer NOT NULL DEFAULT 0,
                PRIMARY KEY (strategy_allocation_id, strategy_opportunity_id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_allocation_opportunity_opportunity_id "
            "ON strategy_allocation_opportunity (strategy_opportunity_id)"
        )
        _log_table("strategy_history", "Strategy run / state history")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_history (
                strategy_history_id bigserial PRIMARY KEY,
                strategy_structure_id bigint REFERENCES strategy_structure(strategy_structure_id),
                ts timestamptz NOT NULL,
                state_summary jsonb,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_history_ts ON strategy_history (ts DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_history_structure_id ON strategy_history (strategy_structure_id)"
        )
        _log("watchlist")
        _log_table("watchlist", "Watchlist items (STK/OPT)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                contract_key text NOT NULL PRIMARY KEY,
                symbol text,
                sec_type text,
                expiry text,
                strike double precision,
                option_right text,
                display_label text,
                source text,
                created_at timestamptz DEFAULT now(),
                category_id integer REFERENCES preference_position_categories(id) ON DELETE SET NULL,
                optionable boolean DEFAULT false
            )
        """
        )

        # DEPRECATED (dbt migration): job_sepa_phase4 is replaced by analytics.sepa_screener_wide
        # in bifrost_golden_source. Phase4 screening now uses analytics tables directly.
        # Will be dropped after SEPA_USE_ANALYTICS is confirmed stable.
        # See: bifrost-analytics/models/marts/
        _log_table("job_sepa_phase4", "SEPA Phase4 async screening job queue")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS job_sepa_phase4 (
                job_sepa_phase4_id bigserial PRIMARY KEY,
                job_id text NOT NULL UNIQUE,
                status text NOT NULL DEFAULT 'queued',
                progress jsonb NOT NULL DEFAULT '{}'::jsonb,
                request jsonb NOT NULL DEFAULT '{}'::jsonb,
                summary jsonb NOT NULL DEFAULT '{}'::jsonb,
                result jsonb,
                errors jsonb NOT NULL DEFAULT '[]'::jsonb,
                created_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now(),
                started_at timestamptz,
                finished_at timestamptz,
                version text NOT NULL DEFAULT 'sepa_phase4_v1'
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_job_sepa_phase4_status_created ON job_sepa_phase4 (status, created_at)"
        )

        # DEPRECATED (dbt migration): stock_readiness_daily is replaced by
        # analytics.sepa_fundamental_eval + analytics.sepa_technical_eval + analytics.sepa_screener_wide
        # in bifrost_golden_source. Will be dropped after SEPA_USE_ANALYTICS is confirmed stable.
        # See: bifrost-analytics/models/marts/
        # Rename legacy table if it still exists under the old sepa-prefixed name
        cur.execute(
            "ALTER TABLE IF EXISTS public.sepa_universe_readiness_daily RENAME TO stock_readiness_daily"
        )
        _log_table(
            "stock_readiness_daily",
            "Stock Data Readiness: daily per-symbol snapshot covering price bars, financials, short data, and SEPA fundamental results",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.stock_readiness_daily (
                as_of_date date NOT NULL,
                symbol text NOT NULL,
                tickers_id bigint NULL,
                universe_rule_version text NOT NULL DEFAULT 'v1',
                price_source text NOT NULL DEFAULT 'massive',
                included_in_universe boolean NOT NULL DEFAULT false,
                bar_count_lookback integer NOT NULL DEFAULT 0,
                first_bar_date date NULL,
                last_bar_date date NULL,
                null_close_rows integer NOT NULL DEFAULT 0,
                null_volume_rows integer NOT NULL DEFAULT 0,
                price_ready boolean NOT NULL DEFAULT false,
                fund_cache_present boolean NOT NULL DEFAULT false,
                fund_cache_expire_at timestamptz NULL,
                notes text NULL,
                computed_at timestamptz NOT NULL DEFAULT now(),
                -- Stage 2: financial statement coverage
                income_stmt_q_count    integer NOT NULL DEFAULT 0,
                income_stmt_a_count    integer NOT NULL DEFAULT 0,
                income_stmt_ready      boolean NOT NULL DEFAULT false,
                balance_sheet_present  boolean NOT NULL DEFAULT false,
                cash_flow_present      boolean NOT NULL DEFAULT false,
                ratios_present         boolean NOT NULL DEFAULT false,
                -- Stage 3: short data coverage
                short_interest_present boolean NOT NULL DEFAULT false,
                short_volume_present   boolean NOT NULL DEFAULT false,
                -- Stage 4: SEPA fundamental results (written directly by run_fundamentals_local_backfill)
                fundamental_pass          boolean NOT NULL DEFAULT false,
                fundamental_pass_count    integer NOT NULL DEFAULT 0,
                fundamental_insufficient  boolean NOT NULL DEFAULT false,
                fundamental_eval         jsonb NULL,
                -- Stage 5: SEPA technical results (written directly by run_technical_local_backfill)
                technical_pass          boolean NOT NULL DEFAULT false,
                technical_pass_count    integer NOT NULL DEFAULT 0,
                technical_insufficient  boolean NOT NULL DEFAULT false,
                technical_eval          jsonb NULL,
                PRIMARY KEY (as_of_date, symbol, universe_rule_version, price_source)
            )
            """
        )
        # Drop obsolete FK to public.tickers if present (market-data owns tickers now).
        cur.execute(
            """
            DO $srd_fk$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'stock_readiness_daily_tickers_id_fkey'
              ) THEN
                ALTER TABLE public.stock_readiness_daily
                  DROP CONSTRAINT stock_readiness_daily_tickers_id_fkey;
              END IF;
            END
            $srd_fk$;
            """
        )
        # ADD COLUMN patches for tables already renamed from the old schema
        for _col_sql in [
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS income_stmt_q_count    integer NOT NULL DEFAULT 0",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS income_stmt_a_count    integer NOT NULL DEFAULT 0",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS income_stmt_ready      boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS balance_sheet_present  boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS cash_flow_present      boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS ratios_present         boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS short_interest_present boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS short_volume_present   boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS fundamental_pass          boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS fundamental_pass_count    integer NOT NULL DEFAULT 0",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS fundamental_insufficient  boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS fundamental_eval         jsonb NULL",
            # Stage 5: SEPA technical evaluation (11 conditions; written by run_technical_local_backfill)
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS technical_pass            boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS technical_pass_count      integer NOT NULL DEFAULT 0",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS technical_insufficient    boolean NOT NULL DEFAULT false",
            "ALTER TABLE public.stock_readiness_daily ADD COLUMN IF NOT EXISTS technical_eval            jsonb NULL",
        ]:
            cur.execute(_col_sql)
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_srd_asof_ready
            ON public.stock_readiness_daily (as_of_date, price_ready)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_srd_asof_symbol
            ON public.stock_readiness_daily (symbol)
            """
        )

        _log("cache_stock_snapshot retired → market.stock_snapshot (Golden Source / Plugin)")
        cur.execute("DROP TABLE IF EXISTS public.cache_stock_snapshot CASCADE")
        # Legacy universe/price_readiness physical tables dropped — now FDW-backed.
        # setup_fdw_market_tables() creates market.ticker FDW + views.
        _log("us_equity_universe / sepa_symbol_price_readiness dropped (FDW-backed)")
        cur.execute("DROP VIEW IF EXISTS public.v_sepa_us_equity_universe CASCADE")
        cur.execute("DROP VIEW IF EXISTS public.v_sepa_symbol_price_readiness CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.us_equity_universe CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.sepa_symbol_price_readiness CASCADE")
        # Rebuild FDW backward-compat view if market.v_us_equity_universe exists
        cur.execute(
            """
            DO $fdw_compat$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM pg_views
                WHERE schemaname = 'market' AND viewname = 'v_us_equity_universe'
              ) THEN
                DROP VIEW IF EXISTS public.v_us_equity_universe CASCADE;
                CREATE VIEW public.v_us_equity_universe AS
                SELECT *,
                       hashtext(upper(trim(symbol)))::bigint AS tickers_id
                FROM market.v_us_equity_universe;
              END IF;
            END $fdw_compat$;
            """
        )
        # DEPRECATED (dbt migration): v_sepa_symbol_fund_cache_readiness is a view on
        # research_sepa_fundamentals_cache which is replaced by analytics.stg_income_stmt
        # (dbt reads directly from market.stock_financials in bifrost_golden_source).
        # Will be dropped after SEPA_USE_ANALYTICS is confirmed stable.
        # See: bifrost-analytics/models/staging/
        _log_table(
            "v_sepa_symbol_fund_cache_readiness",
            "View: valid-row snapshot of research_sepa_fundamentals_cache (created when cache table exists)",
        )
        cur.execute(
            """
            DO $sepa_fund_v$
            BEGIN
              IF to_regclass('public.research_sepa_fundamentals_cache') IS NOT NULL THEN
                EXECUTE $sql$
                CREATE OR REPLACE VIEW public.v_sepa_symbol_fund_cache_readiness AS
                SELECT
                    upper(trim(c.symbol)) AS symbol,
                    c.rule_version,
                    (c.expire_at > now()) AS fund_cache_valid,
                    c.expire_at,
                    c.fetched_at
                FROM public.research_sepa_fundamentals_cache c
                WHERE c.rule_version = 'sepa_fundamentals_v1'
                $sql$;
              END IF;
            END
            $sepa_fund_v$
            """
        )

        # P7: option_trades / max-pain / ATM IV retired — Plugin market_analytics.*.
        _log("option_trades skipped (Market Data Plugin)")
        # Brokerage Golden Source owns executions_raw_* + executions views.
        _log("executions_raw_* / account_executions* views skipped (brokerage.*)")

        # One execution row (unified account_executions_id) may attribute quantity to multiple strategy_instance rows.
        _log_table(
            "account_execution_instance_allocation",
            "Execution to strategy_instance quantity splits (R-A2 extension)",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_execution_instance_allocation (
                account_execution_instance_allocation_id bigserial PRIMARY KEY,
                account_id text NOT NULL,
                account_executions_id bigint NOT NULL,
                strategy_instance_id bigint NOT NULL REFERENCES strategy_instance(strategy_instance_id) ON DELETE RESTRICT,
                allocated_quantity double precision NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (account_executions_id, strategy_instance_id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS account_exec_inst_alloc_account_exec_id "
            "ON account_execution_instance_allocation (account_id, account_executions_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS account_exec_inst_alloc_strategy_instance_id "
            "ON account_execution_instance_allocation (strategy_instance_id)"
        )

        # OPT exercise / assignment: link option execution row(s) to underlying STK fills (performance book).
        _log_table(
            "account_execution_option_stock_link",
            "Option leg to underlying stock execution(s); slippage vs close_price computed in API reader",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_execution_option_stock_link (
                account_execution_option_stock_link_id bigserial PRIMARY KEY,
                account_id text NOT NULL,
                option_account_executions_id bigint NOT NULL,
                stock_account_executions_id bigint NOT NULL,
                role text,
                note text,
                created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (option_account_executions_id, stock_account_executions_id),
                CONSTRAINT account_execution_option_stock_link_role_chk CHECK (
                    role IS NULL OR lower(trim(role)) IN ('exercise', 'assignment')
                )
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS account_exec_opt_stock_link_option "
            "ON account_execution_option_stock_link (account_id, option_account_executions_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS account_exec_opt_stock_link_stock "
            "ON account_execution_option_stock_link (account_id, stock_account_executions_id)"
        )

        conn.commit()
