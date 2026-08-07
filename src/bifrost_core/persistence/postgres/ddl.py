"""PostgreSQL DDL: current schema (CREATE TABLE IF NOT EXISTS + indexes only)."""


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
        _log(
            "daemon_auto_status_current, daemon_auto_status_history, daemon_auto_operations"
        )
        _log_table(
            "daemon_auto_status_current",
            "Daemon auto-trading current status snapshot (single row)",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_auto_status_current (
                daemon_auto_status_current_id integer PRIMARY KEY DEFAULT 1,
                daemon_state text,
                trading_state text,
                symbol text,
                spot double precision,
                bid double precision,
                ask double precision,
                net_delta double precision,
                stock_position integer,
                option_legs_count integer,
                daily_hedge_count integer,
                daily_pnl double precision,
                data_lag_ms double precision,
                config_summary text,
                ts double precision
            )
        """
        )
        _log_table(
            "daemon_auto_status_history", "Daemon auto-trading status snapshot history"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_auto_status_history (
                daemon_auto_status_history_id bigserial PRIMARY KEY,
                daemon_state text,
                trading_state text,
                symbol text,
                spot double precision,
                bid double precision,
                ask double precision,
                net_delta double precision,
                stock_position integer,
                option_legs_count integer,
                daily_hedge_count integer,
                daily_pnl double precision,
                data_lag_ms double precision,
                config_summary text,
                ts double precision
            )
            """
        )
        _log_table("daemon_auto_operations", "Daemon auto-trading operations log")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_auto_operations (
                daemon_auto_operations_id bigserial PRIMARY KEY,
                ts double precision,
                type text,
                side text,
                quantity integer,
                price double precision,
                state_reason text
            )
        """
        )
        _log("daemon_control, daemon_run_status, daemon_heartbeat")
        _log_table("daemon_control", "Daemon control commands (stop, refresh, etc.)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_control (
                id bigserial PRIMARY KEY,
                command text NOT NULL,
                created_at timestamptz DEFAULT now(),
                consumed_at timestamptz
            )
        """
        )
        _log_table(
            "daemon_run_status",
            "Run suspended flag (single row). Default suspended=true so Trading Strategy and IB Trading Client stay off until Resume.",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_run_status (
                id integer PRIMARY KEY DEFAULT 1,
                suspended boolean NOT NULL DEFAULT true,
                updated_at timestamptz DEFAULT now(),
                heartbeat_interval_sec smallint
            )
        """
        )
        cur.execute(
            """
            INSERT INTO daemon_run_status (id, suspended) VALUES (1, true)
            ON CONFLICT (id) DO NOTHING
        """
        )
        _log_table("daemon_heartbeat", "Daemon heartbeat and IB/subscription status")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_heartbeat (
                id integer PRIMARY KEY DEFAULT 1,
                last_ts timestamptz NOT NULL DEFAULT now(),
                hedge_running boolean NOT NULL DEFAULT false,
                ib_connected boolean DEFAULT false,
                ib_client_id integer,
                next_retry_ts timestamptz,
                seconds_until_retry smallint,
                graceful_shutdown_at timestamptz,
                heartbeat_interval_sec smallint,
                redis_quotes_connected boolean DEFAULT false,
                event_subscribe_ticker boolean DEFAULT false,
                event_subscribe_positions boolean DEFAULT false,
                event_subscribe_fills boolean DEFAULT false,
                event_subscribe_commission boolean DEFAULT false,
                listener_connected boolean DEFAULT false,
                listener_client_id integer,
                listener_2_connected boolean DEFAULT false,
                listener_2_client_id integer,
                event_subscribe_positions_ib2 boolean DEFAULT false,
                event_subscribe_fills_ib2 boolean DEFAULT false,
                event_subscribe_commission_ib2 boolean DEFAULT false,
                last_control_message text,
                subscribed_tickers text[],
                mock_hedging boolean DEFAULT true
            )
        """
        )
        cur.execute(
            """
            INSERT INTO daemon_heartbeat (id, last_ts, hedge_running) VALUES (1, now(), false)
            ON CONFLICT (id) DO NOTHING
        """
        )
        _log_table("daemon_open_orders", "R-A5: open/unfilled orders snapshot")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daemon_open_orders (
                id bigserial PRIMARY KEY,
                order_id integer NOT NULL,
                perm_id integer,
                account_id text,
                symbol text,
                sec_type text,
                action text,
                total_quantity numeric,
                filled numeric,
                remaining numeric,
                limit_price numeric,
                status text,
                contract_key text,
                updated_ts timestamptz DEFAULT now()
            )
        """
        )
        _log("account_sync_control, account_sync_run_status, account_sync_heartbeat")
        _log_table("account_sync_control", "Account Sync Daemon control commands")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_sync_control (
                id bigserial PRIMARY KEY,
                command text NOT NULL,
                created_at timestamptz DEFAULT now(),
                consumed_at timestamptz
            )
        """
        )
        _log_table(
            "account_sync_run_status",
            "Account Sync Daemon run status (single row). Default suspended=false.",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_sync_run_status (
                id integer PRIMARY KEY DEFAULT 1,
                suspended boolean NOT NULL DEFAULT false,
                heartbeat_interval_sec real DEFAULT 5.0,
                updated_at timestamptz DEFAULT now()
            )
        """
        )
        cur.execute(
            """
            INSERT INTO account_sync_run_status (id, suspended, heartbeat_interval_sec)
            VALUES (1, false, 5.0)
            ON CONFLICT (id) DO NOTHING
        """
        )
        _log_table("account_sync_heartbeat", "Account Sync Daemon heartbeat and sync stats")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_sync_heartbeat (
                id integer PRIMARY KEY DEFAULT 1,
                last_ts timestamptz,
                last_sync_version bigint DEFAULT 0,
                accounts_synced integer DEFAULT 0,
                positions_synced integer DEFAULT 0,
                executions_synced integer DEFAULT 0,
                open_orders_synced integer DEFAULT 0,
                stream_lag bigint DEFAULT 0,
                updated_at timestamptz DEFAULT now()
            )
        """
        )
        cur.execute(
            """
            INSERT INTO account_sync_heartbeat (id, last_ts) VALUES (1, now())
            ON CONFLICT (id) DO NOTHING
        """
        )

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
        _log("account, account_positions, contract_quote_live")
        _log_table("account", "Account summaries")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account (
                account_id text PRIMARY KEY,
                updated_at timestamptz DEFAULT now(),
                net_liquidation double precision,
                total_cash double precision,
                buying_power double precision,
                summary_extra jsonb
            )
        """
        )
        _log_table("account_positions", "Positions per account")
        # account_positions: (account_id, contract_key) 为主键，无 id；天然按主键 INSERT/UPDATE，仅删除已平仓行
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_positions (
                account_id text NOT NULL,
                contract_key text NOT NULL,
                symbol text,
                sec_type text,
                exchange text,
                currency text,
                position double precision,
                avg_cost double precision,
                expiry text,
                strike double precision,
                option_right text,
                updated_at timestamptz DEFAULT now(),
                PRIMARY KEY (account_id, contract_key)
            )
        """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS account_positions_account_contract_key
            ON account_positions (account_id, contract_key)
        """
        )
        _log_table("contract_quote_live", "Last prices for positions/watchlist")
        # R-M6: 每个持仓标的当前价（按 contract_key 聚合），供监控页逐行展示与计算盈亏
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS contract_quote_live (
                contract_key text PRIMARY KEY,
                symbol text,
                sec_type text,
                expiry text,
                strike double precision,
                option_right text,
                last double precision,
                bid double precision,
                ask double precision,
                mid double precision,
                updated_at timestamptz DEFAULT now()
            )
        """
        )
        _log("account_execution_commissions")
        _log_table("account_execution_commissions", "Commission records")
        # R-A2 §2.11.1: CommissionReport 表，与 account_executions 通过 exec_id 关联
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_execution_commissions (
                exec_id text PRIMARY KEY,
                commission double precision,
                currency text,
                realized_pnl double precision,
                yield_ double precision,
                yield_redemption_date integer,
                created_at timestamptz DEFAULT now()
            )
        """
        )
        # §2.21: account_transactions (Flex cash transactions for Performance Phase 0)
        _log("account_transactions")
        _log_table("account_transactions", "Cash/transaction records")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS account_transactions (
                account_transactions_id bigserial PRIMARY KEY,
                account_id text NOT NULL,
                ts timestamptz NOT NULL,
                amount double precision NOT NULL,
                type text NOT NULL,
                currency text,
                description text,
                flex_transaction_id text,
                flex_type text,
                flex_code text,
                asset_category text,
                asset_subcategory text,
                symbol text,
                conid bigint,
                security_id text,
                security_id_type text,
                listing_exchange text,
                report_date date,
                available_for_trading_date date,
                fx_rate_to_base double precision,
                raw_extra jsonb,
                created_at timestamptz DEFAULT now(),
                UNIQUE(account_id, ts, amount, type)
            )
        """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS account_transactions_account_ts ON account_transactions (account_id, ts DESC)"
        )
        # Market feed tables (stock_day/min, option_*, tickers/ticker_overview, fundamentals,
        # job_massive_backfill, etc.) are owned by bifrost-platform-plugin-market-data
        # (market.* / data_ops.*). Core DDL no longer creates those public tables.

        _log("ticker_types, ticker_related_tickers, job_ticker_reference_state")
        # Rename legacy ticker_types table (idempotent; fresh DBs use CREATE below).
        cur.execute(
            """
            DO $$
            BEGIN
              IF to_regclass('public.ticker_instrument_types') IS NOT NULL
                 AND to_regclass('public.ticker_types') IS NULL THEN
                ALTER TABLE ticker_instrument_types RENAME TO ticker_types;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'ticker_types'
                    AND column_name = 'ticker_instrument_types_id'
                ) THEN
                  ALTER TABLE ticker_types RENAME COLUMN ticker_instrument_types_id TO ticker_types_id;
                END IF;
                IF EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = 'ticker_instrument_types_code') THEN
                  ALTER INDEX ticker_instrument_types_code RENAME TO ticker_types_code;
                END IF;
              END IF;
            END $$;
            """
        )
        _log_table("ticker_types", "Massive ticker instrument type codes (Trade reference)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ticker_types (
                ticker_types_id bigserial PRIMARY KEY,
                code text NOT NULL,
                description text,
                asset_class text NOT NULL DEFAULT '',
                locale text NOT NULL DEFAULT '',
                created_at timestamptz DEFAULT now(),
                UNIQUE (code, asset_class, locale)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ticker_types_code ON ticker_types (code)"
        )
        _log_table("ticker_related_tickers", "Related tickers by from_symbol (no FK to public.tickers)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ticker_related_tickers (
                ticker_related_tickers_id bigserial PRIMARY KEY,
                from_symbol text NOT NULL,
                to_symbol text NOT NULL,
                rank integer NOT NULL DEFAULT 0,
                fetched_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (from_symbol, to_symbol)
            )
            """
        )
        # Existing DBs: migrate from_tickers_id FK → symbol-keyed (P9 drops public.tickers).
        cur.execute(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'ticker_related_tickers'
                  AND column_name = 'from_tickers_id'
              ) AND NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'ticker_related_tickers'
                  AND column_name = 'from_symbol'
              ) THEN
                ALTER TABLE ticker_related_tickers ADD COLUMN from_symbol text;

                IF to_regclass('public.tickers') IS NOT NULL THEN
                  UPDATE ticker_related_tickers r
                  SET from_symbol = t.ticker
                  FROM tickers t
                  WHERE t.tickers_id = r.from_tickers_id;
                END IF;

                DELETE FROM ticker_related_tickers WHERE from_symbol IS NULL;

                ALTER TABLE ticker_related_tickers
                  ALTER COLUMN from_symbol SET NOT NULL;

                -- DROP COLUMN removes FK / UNIQUE / indexes that depend on from_tickers_id
                ALTER TABLE ticker_related_tickers DROP COLUMN from_tickers_id;

                ALTER TABLE ticker_related_tickers
                  ADD CONSTRAINT ticker_related_tickers_from_symbol_to_symbol_key
                  UNIQUE (from_symbol, to_symbol);

                CREATE INDEX IF NOT EXISTS ticker_related_from
                  ON ticker_related_tickers (from_symbol);
              END IF;
            END $$;
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ticker_related_from ON ticker_related_tickers (from_symbol)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ticker_related_to_symbol ON ticker_related_tickers (to_symbol)"
        )
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
        # One-time: migrate legacy stocks / stock_related_tickers / job_stock_reference_state
        # without recreating public.tickers / ticker_overview.
        cur.execute(
            """
            DO $$
            BEGIN
              IF to_regclass('public.stocks') IS NULL THEN
                RETURN;
              END IF;

              IF to_regclass('public.stock_related_tickers') IS NOT NULL THEN
                INSERT INTO ticker_related_tickers (from_symbol, to_symbol, rank, fetched_at)
                SELECT upper(trim(s.symbol)), r.to_symbol, r.rank, r.fetched_at
                FROM stock_related_tickers r
                INNER JOIN stocks s ON s.stocks_id = r.from_stocks_id
                ON CONFLICT (from_symbol, to_symbol) DO NOTHING;
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
        _log("reference_us_holidays")
        _log_table("reference_us_holidays", "US market holidays")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reference_us_holidays (
                exchange text NOT NULL DEFAULT 'NYSE',
                holiday_date date NOT NULL,
                label text,
                name text,
                status text,
                open_time timestamptz,
                close_time timestamptz,
                source text NOT NULL DEFAULT 'manual',
                updated_at timestamptz DEFAULT now(),
                created_at timestamptz DEFAULT now(),
                PRIMARY KEY (exchange, holiday_date)
            )
            """
        )
        cur.execute("ALTER TABLE reference_us_holidays ADD COLUMN IF NOT EXISTS name text")
        cur.execute("ALTER TABLE reference_us_holidays ADD COLUMN IF NOT EXISTS status text")
        cur.execute("ALTER TABLE reference_us_holidays ADD COLUMN IF NOT EXISTS open_time timestamptz")
        cur.execute("ALTER TABLE reference_us_holidays ADD COLUMN IF NOT EXISTS close_time timestamptz")
        cur.execute(
            "ALTER TABLE reference_us_holidays ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'manual'"
        )
        cur.execute(
            "ALTER TABLE reference_us_holidays ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_reference_us_holidays_status ON reference_us_holidays (exchange, status)"
        )
        _log("settings_ib_flex")
        _log_table("settings_ib_flex", "IB Flex query config")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings_ib_flex (
                id serial PRIMARY KEY,
                sort_order integer NOT NULL DEFAULT 0,
                query_label text,
                purpose text DEFAULT 'cash_transactions',
                query_host_id text NOT NULL,
                query_secondary_id text
            )
            """
        )
        # Strategy & gate_safety tables (DATABASE.md §2.24)
        _log("gate_safety_*, strategy_*, settings active_*")
        _log_table("gate_safety_strategy", "Safety boundary set root + strategy layer")
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
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
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
        _log_table("gate_safety_state", "State layer")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_safety_state (
                gate_safety_strategy_id bigint PRIMARY KEY REFERENCES gate_safety_strategy(gate_safety_strategy_id) ON DELETE CASCADE,
                epsilon_band integer NOT NULL,
                threshold_hedge_shares integer NOT NULL,
                max_delta_limit integer NOT NULL,
                vol_window_min integer NOT NULL,
                stale_ts_threshold_ms integer NOT NULL,
                wide_spread_pct double precision NOT NULL,
                extreme_spread_pct double precision NOT NULL,
                data_lag_threshold_ms integer NOT NULL
            )
            """
        )
        _log_table("gate_safety_intent", "Intent layer")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_safety_intent (
                gate_safety_strategy_id bigint PRIMARY KEY REFERENCES gate_safety_strategy(gate_safety_strategy_id) ON DELETE CASCADE,
                min_hedge_shares integer NOT NULL,
                cooldown_seconds integer NOT NULL,
                max_hedge_shares_per_order integer NOT NULL,
                min_price_move_pct double precision NOT NULL
            )
            """
        )
        _log_table("gate_safety_guard", "Guard layer")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_safety_guard (
                gate_safety_strategy_id bigint PRIMARY KEY REFERENCES gate_safety_strategy(gate_safety_strategy_id) ON DELETE CASCADE,
                max_daily_hedge_count integer NOT NULL,
                max_position_shares integer NOT NULL,
                max_daily_loss_usd double precision NOT NULL,
                max_net_delta_shares integer NOT NULL,
                max_spread_pct double precision NOT NULL,
                paper_trade boolean NOT NULL DEFAULT true
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
        _log_table(
            "strategy_structure_constraint",
            "Structure strategy constraint (typed key-value)",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_structure_constraint (
                strategy_structure_constraint_id bigserial PRIMARY KEY,
                strategy_structure_id bigint NOT NULL REFERENCES strategy_structure(strategy_structure_id) ON DELETE CASCADE,
                constraint_type text NOT NULL,
                constraint_value_text text,
                constraint_value_int integer,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS strategy_structure_constraint_structure_id ON strategy_structure_constraint (strategy_structure_id)"
        )
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
        _log("watchlist, job_bars_backfill")
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
        _log_table("job_bars_backfill", "Backfill job queue (Celery worker)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS job_bars_backfill (
                job_bars_backfill_id bigserial PRIMARY KEY,
                symbol text NOT NULL,
                period text NOT NULL DEFAULT '1 D',
                years double precision,
                days integer,
                override_days double precision,
                status text NOT NULL DEFAULT 'pending',
                result jsonb,
                created_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now(),
                skip_ib boolean DEFAULT false,
                api_interval_sec integer DEFAULT 10,
                span_hours double precision
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS job_bars_backfill_status_created ON job_bars_backfill (status, created_at)"
        )

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

        _log_table(
            "cache_stock_snapshot",
            "Massive GET /v3/snapshot (stocks) per-symbol session/last_minute cache for Stock Data Readiness baseline",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.cache_stock_snapshot (
                symbol text NOT NULL,
                fetched_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now(),
                last_minute_updated timestamptz NULL,
                source text NOT NULL DEFAULT 'massive',
                snapshot_asset_type text NULL,
                market_status text NULL,
                snapshot_display_name text NULL,
                session_open double precision NULL,
                session_high double precision NULL,
                session_low double precision NULL,
                session_close double precision NULL,
                session_previous_close double precision NULL,
                session_volume double precision NULL,
                session_decimal_volume text NULL,
                session_change double precision NULL,
                session_change_percent double precision NULL,
                session_regular_trading_change double precision NULL,
                session_regular_trading_change_percent double precision NULL,
                session_early_trading_change double precision NULL,
                session_early_trading_change_percent double precision NULL,
                session_late_trading_change double precision NULL,
                session_late_trading_change_percent double precision NULL,
                last_minute_open double precision NULL,
                last_minute_high double precision NULL,
                last_minute_low double precision NULL,
                last_minute_close double precision NULL,
                last_minute_vwap double precision NULL,
                last_minute_volume double precision NULL,
                last_minute_decimal_volume text NULL,
                last_minute_transactions bigint NULL,
                last_trade_price double precision NULL,
                last_trade_size bigint NULL,
                last_trade_exchange integer NULL,
                last_trade_last_updated_ns bigint NULL,
                last_trade_conditions text NULL,
                last_quote_bid double precision NULL,
                last_quote_ask double precision NULL,
                last_quote_bid_size bigint NULL,
                last_quote_ask_size bigint NULL,
                last_quote_last_updated_ns bigint NULL,
                PRIMARY KEY (symbol)
            )
            """
        )
        for _css_alter in (
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS snapshot_asset_type text",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS market_status text",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS snapshot_display_name text",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_open double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_high double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_low double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_close double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_previous_close double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_volume double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_decimal_volume text",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_change double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_change_percent double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_regular_trading_change double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_regular_trading_change_percent double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_early_trading_change double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_early_trading_change_percent double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_late_trading_change double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS session_late_trading_change_percent double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_minute_open double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_minute_high double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_minute_low double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_minute_close double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_minute_vwap double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_minute_volume double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_minute_decimal_volume text",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_minute_transactions bigint",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_trade_price double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_trade_size bigint",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_trade_exchange integer",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_trade_last_updated_ns bigint",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_trade_conditions text",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_quote_bid double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_quote_ask double precision",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_quote_bid_size bigint",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_quote_ask_size bigint",
            "ALTER TABLE public.cache_stock_snapshot ADD COLUMN IF NOT EXISTS last_quote_last_updated_ns bigint",
        ):
            cur.execute(_css_alter)
        cur.execute("ALTER TABLE public.cache_stock_snapshot DROP COLUMN IF EXISTS session")
        cur.execute("ALTER TABLE public.cache_stock_snapshot DROP COLUMN IF EXISTS last_minute")
        cur.execute("ALTER TABLE public.cache_stock_snapshot DROP COLUMN IF EXISTS payload")
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cache_stock_snapshot_fetched
            ON public.cache_stock_snapshot (fetched_at DESC)
            """
        )
        # P9: Trade-facing SEPA/universe views read market.* (public.tickers / stock_day dropped).
        # Created only when market schema tables exist (plugin DDL applied first).
        _log_table(
            "v_us_equity_universe",
            "View: US common-stock universe from market.ticker (compat shape for SEPA)",
        )
        cur.execute(
            """
            DO $univ$
            BEGIN
              IF to_regclass('market.ticker') IS NOT NULL THEN
                EXECUTE $sql$
                CREATE OR REPLACE VIEW public.v_us_equity_universe AS
                SELECT
                    -- Stable synthetic id (public.tickers dropped in P9). Non-null so
                    -- readiness_snapshot included_in_universe = (tickers_id IS NOT NULL) works.
                    hashtext(upper(trim(t.symbol)))::bigint AS tickers_id,
                    upper(trim(t.symbol)) AS symbol,
                    t.name,
                    t.market,
                    t.locale,
                    t.primary_exchange,
                    t.instrument_type,
                    t.active,
                    NULL::timestamptz AS delisted_utc,
                    t.list_date,
                    t.sector,
                    t.industry
                FROM market.ticker t
                WHERE COALESCE(t.active, false) = true
                  AND lower(COALESCE(t.locale, '')) = 'us'
                  AND lower(COALESCE(t.market, '')) = 'stocks'
                  AND lower(COALESCE(t.instrument_type, '')) = 'cs'
                $sql$;
                EXECUTE 'CREATE OR REPLACE VIEW public.v_sepa_us_equity_universe AS SELECT * FROM public.v_us_equity_universe';
              END IF;
            END
            $univ$
            """
        )
        _log_table(
            "v_sepa_symbol_price_readiness",
            "View: per-symbol market.stock_daily bar counts and price_ready vs lookback window",
        )
        cur.execute(
            """
            DO $price_ready$
            BEGIN
              IF to_regclass('market.stock_daily') IS NOT NULL THEN
                EXECUTE $sql$
                CREATE OR REPLACE VIEW public.v_sepa_symbol_price_readiness AS
                WITH params AS (
                    SELECT
                        'polygon'::text AS price_source,
                        (CURRENT_DATE - integer '420') AS window_start,
                        CURRENT_DATE AS as_of_date,
                        240::integer AS min_bar_rows,
                        7::integer AS max_stale_calendar_days
                )
                SELECT
                    p.as_of_date,
                    upper(trim(sd.symbol)) AS symbol,
                    p.price_source,
                    count(*)::integer AS bar_rows,
                    min(sd.bar_date)::date AS first_bar_date,
                    max(sd.bar_date)::date AS last_bar_date,
                    count(*) FILTER (WHERE sd.close IS NULL)::integer AS null_close_rows,
                    count(*) FILTER (WHERE sd.volume IS NULL)::integer AS null_volume_rows,
                    (
                        count(*) >= p.min_bar_rows
                        AND max(sd.bar_date) >= (
                            p.as_of_date - (p.max_stale_calendar_days || ' days')::interval
                        )::date
                        AND count(*) FILTER (WHERE sd.close IS NULL) = 0
                        AND count(*) FILTER (WHERE sd.volume IS NULL) = 0
                    ) AS price_ready
                FROM params p
                JOIN market.stock_daily sd
                    ON sd.bar_date >= p.window_start
                   AND sd.bar_date <= p.as_of_date
                GROUP BY p.as_of_date, p.price_source, p.min_bar_rows, p.max_stale_calendar_days,
                         p.window_start, upper(trim(sd.symbol))
                $sql$;
              END IF;
            END
            $price_ready$
            """
        )
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

        # P7 (market-data-expand): report_option_max_pain_daily / report_option_atm_iv_daily
        # retired — analytics live in market_analytics.* (Plugin). DROP via
        # bifrost-platform-plugin-market-data/scripts/p7_drop_legacy_tables.sql
        _log_table("option_trades", "Option trades ticks (Massive Developer tier)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS option_trades (
                option_trades_id bigserial PRIMARY KEY,
                contract_key text NOT NULL,
                symbol text NOT NULL,
                expiry text NOT NULL,
                strike double precision NOT NULL,
                option_right text NOT NULL,
                trade_ts timestamptz NOT NULL,
                price double precision NOT NULL,
                size integer NOT NULL,
                exchange text,
                conditions text,
                massive_trade_id text NOT NULL,
                source text NOT NULL DEFAULT 'massive',
                created_at timestamptz DEFAULT now(),
                UNIQUE (massive_trade_id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS option_trades_contract_ts ON option_trades (contract_key, trade_ts DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS option_trades_symbol_ts ON option_trades (symbol, trade_ts DESC)"
        )
        # ── Executions: raw tables + account_executions view ──
        # TWS and Flex stored separately; view merges (Flex authoritative, TWS fills gaps).
        # Match key: exec_id (IB Execution ID = Flex ibExecID).
        _log("executions_raw_tws, executions_raw_flex, account_executions(view)")
        _log_table("executions_raw_tws", "Raw TWS/manual executions (tws_event, tws_client, manual)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS executions_raw_tws (
                executions_raw_tws_id bigserial PRIMARY KEY,
                account_id text,
                exec_id text,
                exec_time timestamptz,
                symbol text,
                sec_type text,
                side text,
                quantity double precision,
                price double precision,
                source text,
                expiry text,
                strike double precision,
                option_right text,
                exchange text,
                order_id bigint,
                cum_qty double precision,
                contract_key text,
                currency text,
                asset_category text,
                sub_category text,
                description text,
                conid bigint,
                security_id text,
                security_id_type text,
                cusip text,
                isin text,
                figi text,
                listing_exchange text,
                underlying_conid bigint,
                underlying_symbol text,
                underlying_security_id text,
                underlying_listing_exchange text,
                issuer text,
                issuer_country_code text,
                trade_id text,
                related_trade_id text,
                report_date date,
                trade_date date,
                settle_date_target date,
                transaction_type text,
                multiplier double precision,
                principal_adjust_factor text,
                proceeds double precision,
                taxes double precision,
                net_cash double precision,
                close_price double precision,
                open_close_indicator text,
                notes text,
                cost double precision,
                fifo_pnl_realized double precision,
                mtm_pnl double precision,
                trade_money double precision,
                fx_rate_to_base double precision,
                acct_alias text,
                model text,
                raw_extra jsonb,
                strategy_opportunity_id bigint,
                strategy_instance_id bigint,
                legacy_account_executions_id bigint,
                created_at timestamptz DEFAULT now()
            )
        """
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS executions_raw_tws_exec_id_key "
            "ON executions_raw_tws (exec_id) WHERE exec_id IS NOT NULL AND exec_id != ''"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_tws_account_time "
            "ON executions_raw_tws (account_id, exec_time DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_tws_contract_key "
            "ON executions_raw_tws (account_id, contract_key) WHERE contract_key IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_tws_strategy_opportunity_id "
            "ON executions_raw_tws (strategy_opportunity_id) WHERE strategy_opportunity_id IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_tws_strategy_instance_id "
            "ON executions_raw_tws (strategy_instance_id) WHERE strategy_instance_id IS NOT NULL"
        )

        _log_table("executions_raw_flex", "Raw Flex executions (flex_trades source, authoritative)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS executions_raw_flex (
                executions_raw_flex_id bigserial PRIMARY KEY,
                account_id text,
                exec_id text,
                exec_time timestamptz,
                symbol text,
                sec_type text,
                side text,
                quantity double precision,
                price double precision,
                source text DEFAULT 'flex_trades',
                expiry text,
                strike double precision,
                option_right text,
                exchange text,
                order_id bigint,
                cum_qty double precision,
                contract_key text,
                currency text,
                asset_category text,
                sub_category text,
                description text,
                conid bigint,
                security_id text,
                security_id_type text,
                cusip text,
                isin text,
                figi text,
                listing_exchange text,
                underlying_conid bigint,
                underlying_symbol text,
                underlying_security_id text,
                underlying_listing_exchange text,
                issuer text,
                issuer_country_code text,
                trade_id text,
                related_trade_id text,
                report_date date,
                trade_date date,
                settle_date_target date,
                transaction_type text,
                multiplier double precision,
                principal_adjust_factor text,
                proceeds double precision,
                taxes double precision,
                net_cash double precision,
                close_price double precision,
                open_close_indicator text,
                notes text,
                cost double precision,
                fifo_pnl_realized double precision,
                mtm_pnl double precision,
                trade_money double precision,
                fx_rate_to_base double precision,
                acct_alias text,
                model text,
                raw_extra jsonb,
                strategy_opportunity_id bigint,
                strategy_instance_id bigint,
                legacy_account_executions_id bigint,
                created_at timestamptz DEFAULT now()
            )
        """
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS executions_raw_flex_exec_id_key "
            "ON executions_raw_flex (exec_id) WHERE exec_id IS NOT NULL AND exec_id != ''"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS executions_raw_flex_account_trade_id_key "
            "ON executions_raw_flex (account_id, trade_id) WHERE trade_id IS NOT NULL AND trade_id != ''"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_flex_account_time "
            "ON executions_raw_flex (account_id, exec_time DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_flex_contract_key "
            "ON executions_raw_flex (account_id, contract_key) WHERE contract_key IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_flex_trade_date "
            "ON executions_raw_flex (account_id, trade_date DESC) WHERE trade_date IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_flex_strategy_opportunity_id "
            "ON executions_raw_flex (strategy_opportunity_id) WHERE strategy_opportunity_id IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_flex_strategy_instance_id "
            "ON executions_raw_flex (strategy_instance_id) WHERE strategy_instance_id IS NOT NULL"
        )

        _log_table("executions_raw_journal", "Raw journal/manual-accounting executions (journal_closed, manual adjustments)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS executions_raw_journal (
                executions_raw_journal_id bigserial PRIMARY KEY,
                account_id text,
                exec_id text,
                exec_time timestamptz,
                symbol text,
                sec_type text,
                side text,
                quantity double precision,
                price double precision,
                source text,
                expiry text,
                strike double precision,
                option_right text,
                exchange text,
                order_id bigint,
                cum_qty double precision,
                contract_key text,
                currency text,
                asset_category text,
                sub_category text,
                description text,
                conid bigint,
                security_id text,
                security_id_type text,
                cusip text,
                isin text,
                figi text,
                listing_exchange text,
                underlying_conid bigint,
                underlying_symbol text,
                underlying_security_id text,
                underlying_listing_exchange text,
                issuer text,
                issuer_country_code text,
                trade_id text,
                related_trade_id text,
                report_date date,
                trade_date date,
                settle_date_target date,
                transaction_type text,
                multiplier double precision,
                principal_adjust_factor text,
                proceeds double precision,
                taxes double precision,
                net_cash double precision,
                close_price double precision,
                open_close_indicator text,
                notes text,
                cost double precision,
                fifo_pnl_realized double precision,
                mtm_pnl double precision,
                trade_money double precision,
                fx_rate_to_base double precision,
                acct_alias text,
                model text,
                raw_extra jsonb,
                strategy_opportunity_id bigint,
                strategy_instance_id bigint,
                legacy_account_executions_id bigint,
                created_at timestamptz DEFAULT now()
            )
        """
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS executions_raw_journal_exec_id_key "
            "ON executions_raw_journal (exec_id) WHERE exec_id IS NOT NULL AND exec_id != ''"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_journal_account_time "
            "ON executions_raw_journal (account_id, exec_time DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_journal_contract_key "
            "ON executions_raw_journal (account_id, contract_key) WHERE contract_key IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_journal_strategy_opportunity_id "
            "ON executions_raw_journal (strategy_opportunity_id) WHERE strategy_opportunity_id IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS executions_raw_journal_strategy_instance_id "
            "ON executions_raw_journal (strategy_instance_id) WHERE strategy_instance_id IS NOT NULL"
        )

        _EXEC_CANONICAL_COLS = (
            "account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, "
            "expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, "
            "currency, asset_category, sub_category, description, conid, "
            "security_id, security_id_type, cusip, isin, figi, listing_exchange, "
            "underlying_conid, underlying_symbol, underlying_security_id, underlying_listing_exchange, "
            "issuer, issuer_country_code, trade_id, related_trade_id, report_date, trade_date, "
            "settle_date_target, transaction_type, multiplier, principal_adjust_factor, "
            "proceeds, taxes, net_cash, close_price, open_close_indicator, notes, cost, "
            "fifo_pnl_realized, mtm_pnl, trade_money, fx_rate_to_base, acct_alias, model, "
            "raw_extra, strategy_opportunity_id, strategy_instance_id, created_at"
        )
        cur.execute("DROP VIEW IF EXISTS executions_canonical")
        cur.execute(
            """
            SELECT c.relkind
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = 'account_executions'
            LIMIT 1
            """
        )
        _ae_rel = cur.fetchone()
        _ae_relkind = _ae_rel[0] if _ae_rel else None
        if _ae_relkind == "v":
            cur.execute("DROP VIEW account_executions")
        elif _ae_relkind == "r":
            cur.execute("DROP TABLE account_executions")
        cur.execute(
            f"""
            CREATE OR REPLACE VIEW account_executions AS
            SELECT executions_raw_flex_id AS account_executions_id,
                   {_EXEC_CANONICAL_COLS}
            FROM executions_raw_flex
            UNION ALL
            SELECT -(executions_raw_tws_id) AS account_executions_id,
                   {_EXEC_CANONICAL_COLS}
            FROM executions_raw_tws t
            WHERE NOT EXISTS (
                SELECT 1 FROM executions_raw_flex f
                WHERE f.exec_id = t.exec_id
                  AND f.exec_id IS NOT NULL AND f.exec_id != ''
                  AND t.exec_id IS NOT NULL AND t.exec_id != ''
            )
            UNION ALL
            SELECT -(1000000000 + executions_raw_journal_id) AS account_executions_id,
                   {_EXEC_CANONICAL_COLS}
            FROM executions_raw_journal
        """
        )
        # Performance-book subset: Flex (authoritative fills) + journal adjustments only (no TWS gap-fill).
        cur.execute(
            f"""
            CREATE OR REPLACE VIEW account_executions_final AS
            SELECT executions_raw_flex_id AS account_executions_id,
                   {_EXEC_CANONICAL_COLS}
            FROM executions_raw_flex
            UNION ALL
            SELECT -(1000000000 + executions_raw_journal_id) AS account_executions_id,
                   {_EXEC_CANONICAL_COLS}
            FROM executions_raw_journal
        """
        )
        _log("account_executions_final(view: flex + journal only)")

        # TWS-only "on the fly" rows: drop any TWS execution already covered by account_executions_final.
        # Match (1) exact contract_key trim equality; (2) STK rows where keys differ only in trailing
        # pipes — IB builds "SYM|STK||||" while Flex uses "SYM|STK|||"; (3) same account + symbol when
        # TWS sec_type is STK and final row is equity-like (e.g. Flex assetCategory FUND vs IB STK).
        _exec_canonical_cols_t = ", ".join(
            f"t.{c.strip()}" for c in _EXEC_CANONICAL_COLS.split(",") if c.strip()
        )
        # Equity-like final rows (excludes OPT — same ticker can name a stock and an option).
        _fly_final_equity_sec_types = (
            "'STK', 'EQUITY', 'FUND', 'ETF', 'ETN', 'ADR', 'CORP', 'STOCK', 'REIT', 'WAR'"
        )
        # Prefer f.sec_type; if null/blank (some Flex rows), infer from contract_key segment 2.
        _fly_f_sec_norm = (
            "upper(trim(COALESCE("
            "NULLIF(trim(COALESCE(f.sec_type, '')), ''), "
            "NULLIF(trim(split_part(COALESCE(f.contract_key, ''), '|', 2)), '')"
            ")))"
        )
        cur.execute(
            f"""
            CREATE OR REPLACE VIEW account_executions_fly AS
            SELECT -(t.executions_raw_tws_id) AS account_executions_id,
                   {_exec_canonical_cols_t}
            FROM executions_raw_tws t
            WHERE upper(trim(COALESCE(t.sec_type, ''))) <> 'BAG'
              AND NOT EXISTS (
                SELECT 1
                FROM account_executions_final f
                WHERE f.account_id IS NOT DISTINCT FROM t.account_id
                  AND (
                    (
                      NULLIF(trim(COALESCE(t.contract_key, '')), '') IS NOT NULL
                      AND NULLIF(trim(COALESCE(f.contract_key, '')), '') IS NOT NULL
                      AND trim(COALESCE(f.contract_key, '')) = trim(COALESCE(t.contract_key, ''))
                    )
                    OR (
                      upper(trim(COALESCE(t.sec_type, ''))) = 'STK'
                      AND upper(trim(COALESCE(f.sec_type, ''))) = 'STK'
                      AND NULLIF(trim(COALESCE(t.contract_key, '')), '') IS NOT NULL
                      AND NULLIF(trim(COALESCE(f.contract_key, '')), '') IS NOT NULL
                      AND rtrim(trim(COALESCE(t.contract_key, '')), '|') = rtrim(trim(COALESCE(f.contract_key, '')), '|')
                    )
                    OR (
                      upper(trim(COALESCE(t.sec_type, ''))) = 'STK'
                      AND {_fly_f_sec_norm} IN ({_fly_final_equity_sec_types})
                      AND NULLIF(trim(COALESCE(t.symbol, '')), '') IS NOT NULL
                      AND upper(trim(COALESCE(t.symbol, ''))) = upper(trim(COALESCE(f.symbol, '')))
                    )
                  )
            )
        """
        )
        _log("account_executions_fly(view: TWS minus final-covered contracts, no BAG)")

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
