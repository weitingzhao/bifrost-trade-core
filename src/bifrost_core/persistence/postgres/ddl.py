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
        _log("stock_day table + index (may block if API/worker use stock_day)")
        _log_table("stock_day", "Stock daily OHLC bars")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_day (
                symbol text NOT NULL,
                bar_time date NOT NULL,
                open double precision,
                high double precision,
                low double precision,
                close double precision,
                volume double precision,
                created_at timestamptz DEFAULT now(),
                source text NOT NULL DEFAULT 'ib',
                CONSTRAINT stock_day_symbol_bar_time_source_key PRIMARY KEY (symbol, bar_time, source)
            ) PARTITION BY RANGE (bar_time)
        """
        )
        # Migrate: if bar_time is still timestamptz from an older schema, convert to date.
        cur.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'stock_day'
                      AND column_name = 'bar_time'
                      AND data_type = 'timestamp with time zone'
                ) THEN
                    TRUNCATE stock_day;
                    ALTER TABLE stock_day ALTER COLUMN bar_time TYPE date USING bar_time::date;
                END IF;
            END $$
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS stock_day_symbol_time ON stock_day (symbol, bar_time DESC)"
        )
        # R-A3 + Massive: source dimension (ib / tv / massive), extended OHLC fields
        cur.execute(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'stock_day'
              ) THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_day' AND column_name = 'source'
                ) THEN
                  ALTER TABLE stock_day ADD COLUMN source text;
                  UPDATE stock_day SET source = 'ib' WHERE source IS NULL;
                  ALTER TABLE stock_day ALTER COLUMN source SET NOT NULL;
                  ALTER TABLE stock_day ALTER COLUMN source SET DEFAULT 'ib';
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_day' AND column_name = 'vwap'
                ) THEN
                  ALTER TABLE stock_day ADD COLUMN vwap double precision;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_day' AND column_name = 'trade_count'
                ) THEN
                  ALTER TABLE stock_day ADD COLUMN trade_count bigint;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_day' AND column_name = 'adjusted'
                ) THEN
                  ALTER TABLE stock_day ADD COLUMN adjusted boolean;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_day' AND column_name = 'extras'
                ) THEN
                  ALTER TABLE stock_day ADD COLUMN extras jsonb;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM pg_constraint
                  WHERE conname = 'stock_day_symbol_bar_time_key'
                ) THEN
                  ALTER TABLE stock_day DROP CONSTRAINT stock_day_symbol_bar_time_key;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM pg_constraint WHERE conname = 'stock_day_symbol_bar_time_source_key'
                ) THEN
                  ALTER TABLE stock_day ADD CONSTRAINT stock_day_symbol_bar_time_source_key
                    UNIQUE (symbol, bar_time, source);
                END IF;
              END IF;
            END $$
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS stock_day_symbol_source_time ON stock_day (symbol, source, bar_time DESC)"
        )
        cur.execute(
            """
            DO $stock_day_part$
            DECLARE
              rk char;
              m_start date;
              m_end date;
              part_name text;
              r record;
              min_bt date;
              max_bt date;
            BEGIN
              SELECT c.relkind INTO rk FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'public' AND c.relname = 'stock_day';
              IF rk IS NULL THEN
                RETURN;
              END IF;

              IF rk = 'p' THEN
                SELECT date_trunc('month', COALESCE((SELECT MIN(bar_time) FROM public.stock_day), CURRENT_DATE))::date
                  INTO m_start;
                max_bt := COALESCE((SELECT MAX(bar_time) FROM public.stock_day), CURRENT_DATE);
                m_end := (date_trunc('month', GREATEST(max_bt, CURRENT_DATE))::date + interval '4 months')::date;
                WHILE m_start < m_end LOOP
                  part_name := 'stock_day_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                  IF to_regclass('public.' || part_name) IS NULL THEN
                    EXECUTE format(
                      'CREATE TABLE %I PARTITION OF public.stock_day FOR VALUES FROM (%L) TO (%L)',
                      part_name, m_start, (m_start + interval '1 month')::date
                    );
                  END IF;
                  m_start := (m_start + interval '1 month')::date;
                END LOOP;
                IF to_regclass('public.stock_day_default') IS NULL THEN
                  CREATE TABLE stock_day_default PARTITION OF public.stock_day DEFAULT;
                END IF;
                RETURN;
              END IF;

              RAISE NOTICE 'Migrating stock_day heap to RANGE partitions on bar_time (date) ...';
              DROP VIEW IF EXISTS public.option_snapshots_with_underlying_day;

              CREATE TABLE stock_day_new (
                symbol text NOT NULL,
                bar_time date NOT NULL,
                open double precision,
                high double precision,
                low double precision,
                close double precision,
                volume double precision,
                created_at timestamptz DEFAULT now(),
                source text NOT NULL DEFAULT 'ib',
                vwap double precision,
                trade_count bigint,
                adjusted boolean,
                extras jsonb,
                CONSTRAINT stock_day_mig_pkey PRIMARY KEY (symbol, bar_time, source)
              ) PARTITION BY RANGE (bar_time);

              SELECT MIN(bar_time), MAX(bar_time) INTO min_bt, max_bt FROM public.stock_day;
              m_start := date_trunc('month', COALESCE(min_bt, CURRENT_DATE))::date;
              IF max_bt IS NULL THEN
                max_bt := COALESCE(min_bt, CURRENT_DATE);
              END IF;
              m_end := (date_trunc('month', GREATEST(max_bt, CURRENT_DATE))::date + interval '4 months')::date;
              WHILE m_start < m_end LOOP
                part_name := 'stock_day_new_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                EXECUTE format(
                  'CREATE TABLE %I PARTITION OF stock_day_new FOR VALUES FROM (%L) TO (%L)',
                  part_name, m_start, (m_start + interval '1 month')::date
                );
                m_start := (m_start + interval '1 month')::date;
              END LOOP;
              CREATE TABLE stock_day_new_default PARTITION OF stock_day_new DEFAULT;

              INSERT INTO stock_day_new (
                symbol, bar_time, open, high, low, close, volume, created_at,
                source, vwap, trade_count, adjusted, extras
              )
              SELECT
                symbol, bar_time::date, open, high, low, close, volume, created_at,
                COALESCE(source, 'ib'), vwap, trade_count, adjusted, extras
              FROM public.stock_day;

              DROP TABLE public.stock_day;
              ALTER TABLE stock_day_new RENAME TO stock_day;

              FOR r IN
                SELECT c.relname AS tname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname LIKE 'stock_day_new_y%'
              LOOP
                EXECUTE format(
                  'ALTER TABLE %I RENAME TO %I',
                  r.tname,
                  replace(r.tname, 'stock_day_new_', 'stock_day_')
                );
              END LOOP;
              IF to_regclass('public.stock_day_new_default') IS NOT NULL THEN
                ALTER TABLE stock_day_new_default RENAME TO stock_day_default;
              END IF;

              ALTER TABLE public.stock_day RENAME CONSTRAINT stock_day_mig_pkey TO stock_day_symbol_bar_time_source_key;

              CREATE INDEX IF NOT EXISTS stock_day_symbol_time ON public.stock_day (symbol, bar_time DESC);
              CREATE INDEX IF NOT EXISTS stock_day_symbol_source_time ON public.stock_day (symbol, source, bar_time DESC);
              RAISE NOTICE 'stock_day: RANGE partition migration complete.';
            END
            $stock_day_part$;
            """
        )
        _log("stock_min table + index")
        _log_table("stock_min", "Stock minute OHLC bars")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_min (
                symbol text NOT NULL,
                period text NOT NULL,
                bar_time timestamptz NOT NULL,
                open double precision,
                high double precision,
                low double precision,
                close double precision,
                volume double precision,
                created_at timestamptz DEFAULT now(),
                source text NOT NULL DEFAULT 'ib',
                CONSTRAINT stock_min_symbol_period_bar_time_source_key PRIMARY KEY (symbol, period, bar_time, source)
            ) PARTITION BY RANGE (bar_time)
        """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS stock_min_symbol_period_time ON stock_min (symbol, period, bar_time DESC)"
        )
        cur.execute(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'stock_min'
              ) THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_min' AND column_name = 'source'
                ) THEN
                  ALTER TABLE stock_min ADD COLUMN source text;
                  UPDATE stock_min SET source = 'ib' WHERE source IS NULL;
                  ALTER TABLE stock_min ALTER COLUMN source SET NOT NULL;
                  ALTER TABLE stock_min ALTER COLUMN source SET DEFAULT 'ib';
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_min' AND column_name = 'vwap'
                ) THEN
                  ALTER TABLE stock_min ADD COLUMN vwap double precision;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_min' AND column_name = 'trade_count'
                ) THEN
                  ALTER TABLE stock_min ADD COLUMN trade_count bigint;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_min' AND column_name = 'adjusted'
                ) THEN
                  ALTER TABLE stock_min ADD COLUMN adjusted boolean;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_min' AND column_name = 'extras'
                ) THEN
                  ALTER TABLE stock_min ADD COLUMN extras jsonb;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM pg_constraint
                  WHERE conname = 'stock_min_symbol_period_bar_time_key'
                ) THEN
                  ALTER TABLE stock_min DROP CONSTRAINT stock_min_symbol_period_bar_time_key;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM pg_constraint WHERE conname = 'stock_min_symbol_period_bar_time_source_key'
                ) THEN
                  ALTER TABLE stock_min ADD CONSTRAINT stock_min_symbol_period_bar_time_source_key
                    UNIQUE (symbol, period, bar_time, source);
                END IF;
              END IF;
            END $$
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS stock_min_sym_per_src_time ON stock_min (symbol, period, source, bar_time DESC)"
        )
        cur.execute(
            """
            DO $stock_min_part$
            DECLARE
              rk char;
              m_start date;
              m_end date;
              part_name text;
              r record;
              min_bt timestamptz;
              max_bt timestamptz;
            BEGIN
              SELECT c.relkind INTO rk FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'public' AND c.relname = 'stock_min';
              IF rk IS NULL THEN
                RETURN;
              END IF;

              IF rk = 'p' THEN
                SELECT date_trunc('month', COALESCE((SELECT MIN(bar_time) FROM public.stock_min), now()))::date
                  INTO m_start;
                max_bt := COALESCE((SELECT MAX(bar_time) FROM public.stock_min), now());
                m_end := (date_trunc('month', GREATEST(max_bt, now()))::date + interval '4 months')::date;
                WHILE m_start < m_end LOOP
                  part_name := 'stock_min_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                  IF to_regclass('public.' || part_name) IS NULL THEN
                    EXECUTE format(
                      'CREATE TABLE %I PARTITION OF public.stock_min FOR VALUES FROM (%L) TO (%L)',
                      part_name, m_start, (m_start + interval '1 month')::date
                    );
                  END IF;
                  m_start := (m_start + interval '1 month')::date;
                END LOOP;
                IF to_regclass('public.stock_min_default') IS NULL THEN
                  CREATE TABLE stock_min_default PARTITION OF public.stock_min DEFAULT;
                END IF;
                RETURN;
              END IF;

              RAISE NOTICE 'Migrating stock_min heap to RANGE partitions on bar_time ...';

              CREATE TABLE stock_min_new (
                symbol text NOT NULL,
                period text NOT NULL,
                bar_time timestamptz NOT NULL,
                open double precision,
                high double precision,
                low double precision,
                close double precision,
                volume double precision,
                created_at timestamptz DEFAULT now(),
                source text NOT NULL DEFAULT 'ib',
                vwap double precision,
                trade_count bigint,
                adjusted boolean,
                extras jsonb,
                CONSTRAINT stock_min_mig_pkey PRIMARY KEY (symbol, period, bar_time, source)
              ) PARTITION BY RANGE (bar_time);

              SELECT MIN(bar_time), MAX(bar_time) INTO min_bt, max_bt FROM public.stock_min;
              m_start := date_trunc('month', COALESCE(min_bt, now()))::date;
              IF max_bt IS NULL THEN
                max_bt := COALESCE(min_bt, now());
              END IF;
              m_end := (date_trunc('month', GREATEST(max_bt, now()))::date + interval '4 months')::date;
              WHILE m_start < m_end LOOP
                part_name := 'stock_min_new_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                EXECUTE format(
                  'CREATE TABLE %I PARTITION OF stock_min_new FOR VALUES FROM (%L) TO (%L)',
                  part_name, m_start, (m_start + interval '1 month')::date
                );
                m_start := (m_start + interval '1 month')::date;
              END LOOP;
              CREATE TABLE stock_min_new_default PARTITION OF stock_min_new DEFAULT;

              INSERT INTO stock_min_new (
                symbol, period, bar_time, open, high, low, close, volume, created_at,
                source, vwap, trade_count, adjusted, extras
              )
              SELECT
                symbol, period, bar_time, open, high, low, close, volume, created_at,
                COALESCE(source, 'ib'), vwap, trade_count, adjusted, extras
              FROM public.stock_min;

              DROP TABLE public.stock_min;
              ALTER TABLE stock_min_new RENAME TO stock_min;

              FOR r IN
                SELECT c.relname AS tname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname LIKE 'stock_min_new_y%'
              LOOP
                EXECUTE format(
                  'ALTER TABLE %I RENAME TO %I',
                  r.tname,
                  replace(r.tname, 'stock_min_new_', 'stock_min_')
                );
              END LOOP;
              IF to_regclass('public.stock_min_new_default') IS NOT NULL THEN
                ALTER TABLE stock_min_new_default RENAME TO stock_min_default;
              END IF;

              ALTER TABLE public.stock_min RENAME CONSTRAINT stock_min_mig_pkey TO stock_min_symbol_period_bar_time_source_key;

              CREATE INDEX IF NOT EXISTS stock_min_symbol_period_time ON public.stock_min (symbol, period, bar_time DESC);
              CREATE INDEX IF NOT EXISTS stock_min_sym_per_src_time ON public.stock_min (symbol, period, source, bar_time DESC);
              RAISE NOTICE 'stock_min: RANGE partition migration complete.';
            END
            $stock_min_part$;
            """
        )
        _log("tickers table (Massive reference universe)")
        _log_table("tickers", "Ticker symbol reference (All Tickers)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS tickers (
                tickers_id bigserial PRIMARY KEY,
                ticker text NOT NULL UNIQUE,
                name text,
                market text,
                locale text,
                primary_exchange text,
                instrument_type text,
                active boolean,
                currency_name text,
                currency_symbol text,
                base_currency_name text,
                base_currency_symbol text,
                cik text,
                composite_figi text,
                share_class_figi text,
                last_updated_utc timestamptz,
                delisted_utc timestamptz,
                created_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS tickers_ticker ON tickers (ticker)")
        # Rename legacy Massive reference tables (idempotent; fresh DBs use CREATE below).
        cur.execute(
            """
            DO $$
            BEGIN
              IF to_regclass('public.ticker_reference_details') IS NOT NULL
                 AND to_regclass('public.ticker_overview') IS NULL THEN
                ALTER TABLE ticker_reference_details RENAME TO ticker_overview;
              END IF;
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ticker_overview (
                tickers_id bigint PRIMARY KEY REFERENCES tickers(tickers_id) ON DELETE CASCADE,
                sector text NOT NULL DEFAULT '',
                industry text NOT NULL DEFAULT '',
                exchange text,
                list_date date,
                ticker_root text,
                ticker_suffix text,
                sic_code text,
                sic_description text,
                market_cap double precision,
                total_employees integer,
                address_line1 text,
                address_city text,
                address_state text,
                postal_code text,
                phone text,
                description text,
                homepage_url text,
                icon_url text,
                logo_url text,
                round_lot bigint,
                share_class_shares_outstanding double precision,
                weighted_shares_outstanding double precision,
                overview_api_request_id text,
                overview_api_status text,
                overview_api_count integer,
                overview_updated_at timestamptz
            )
            """
        )
        cur.execute(
            "ALTER TABLE ticker_overview ADD COLUMN IF NOT EXISTS ticker_suffix text"
        )
        cur.execute("ALTER TABLE ticker_overview ADD COLUMN IF NOT EXISTS sic_code text")
        cur.execute(
            "ALTER TABLE ticker_overview ADD COLUMN IF NOT EXISTS homepage_url text"
        )
        cur.execute(
            "ALTER TABLE ticker_overview ADD COLUMN IF NOT EXISTS round_lot bigint"
        )
        cur.execute(
            "ALTER TABLE ticker_overview ADD COLUMN IF NOT EXISTS share_class_shares_outstanding double precision"
        )
        cur.execute(
            "ALTER TABLE ticker_overview ADD COLUMN IF NOT EXISTS weighted_shares_outstanding double precision"
        )
        cur.execute(
            "ALTER TABLE ticker_overview ADD COLUMN IF NOT EXISTS overview_api_request_id text"
        )
        cur.execute(
            "ALTER TABLE ticker_overview ADD COLUMN IF NOT EXISTS overview_api_status text"
        )
        cur.execute(
            "ALTER TABLE ticker_overview ADD COLUMN IF NOT EXISTS overview_api_count integer"
        )
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ticker_related_tickers (
                ticker_related_tickers_id bigserial PRIMARY KEY,
                from_tickers_id bigint NOT NULL REFERENCES tickers(tickers_id) ON DELETE CASCADE,
                to_symbol text NOT NULL,
                rank integer NOT NULL DEFAULT 0,
                fetched_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (from_tickers_id, to_symbol)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ticker_related_from ON ticker_related_tickers (from_tickers_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ticker_related_to_symbol ON ticker_related_tickers (to_symbol)"
        )
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
        cur.execute(
            "CREATE INDEX IF NOT EXISTS tickers_active ON tickers (active) WHERE active IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS tickers_primary_exchange ON tickers (primary_exchange)"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS tickers_instrument_type ON tickers (instrument_type)")
        # One-time: migrate legacy ``stocks`` / ``stock_related_tickers`` / ``job_stock_reference_state`` → new tables.
        cur.execute(
            """
            DO $$
            BEGIN
              IF to_regclass('public.stocks') IS NULL THEN
                RETURN;
              END IF;
              IF EXISTS (SELECT 1 FROM tickers LIMIT 1) THEN
                DROP TABLE IF EXISTS stock_related_tickers CASCADE;
                DROP TABLE IF EXISTS stocks CASCADE;
                DROP TABLE IF EXISTS job_stock_reference_state CASCADE;
                RETURN;
              END IF;

              INSERT INTO tickers (
                ticker, name, market, locale, primary_exchange, instrument_type, active,
                currency_name, currency_symbol, base_currency_name, base_currency_symbol,
                cik, composite_figi, share_class_figi, last_updated_utc, delisted_utc,
                created_at, updated_at
              )
              SELECT
                upper(trim(symbol)),
                name,
                market,
                locale,
                primary_exchange,
                instrument_type,
                active,
                currency_name,
                NULL,
                NULL,
                NULL,
                cik,
                composite_figi,
                share_class_figi,
                NULL,
                NULL,
                COALESCE(created_at, now()),
                COALESCE(reference_updated_at, created_at, now())
              FROM stocks;

              INSERT INTO ticker_overview (
                tickers_id, sector, industry, exchange, list_date, ticker_root, sic_description,
                market_cap, total_employees, address_line1, address_city, address_state, postal_code,
                phone, description, icon_url, logo_url, overview_updated_at
              )
              SELECT
                t.tickers_id,
                '',
                '',
                s.exchange,
                s.list_date,
                s.ticker_root,
                s.sic_description,
                s.market_cap,
                s.total_employees,
                s.address_line1,
                s.address_city,
                s.address_state,
                s.postal_code,
                s.phone,
                s.description,
                s.icon_url,
                s.logo_url,
                COALESCE(s.reference_updated_at, now())
              FROM tickers t
              INNER JOIN stocks s ON t.ticker = upper(trim(s.symbol));

              IF to_regclass('public.stock_related_tickers') IS NOT NULL THEN
                INSERT INTO ticker_related_tickers (from_tickers_id, to_symbol, rank, fetched_at)
                SELECT t.tickers_id, r.to_symbol, r.rank, r.fetched_at
                FROM stock_related_tickers r
                INNER JOIN stocks s ON s.stocks_id = r.from_stocks_id
                INNER JOIN tickers t ON t.ticker = upper(trim(s.symbol));
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
        _log("option_day, option_min tables + indexes")
        _log_table("option_day", "Option daily OHLC bars")
        cur.execute("CREATE SEQUENCE IF NOT EXISTS option_day_option_day_id_seq AS bigint")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS option_day (
                option_day_id bigint NOT NULL DEFAULT nextval('option_day_option_day_id_seq'),
                symbol text NOT NULL,
                expiry text NOT NULL,
                strike double precision NOT NULL,
                option_right text NOT NULL,
                bar_time timestamptz NOT NULL,
                open double precision,
                high double precision,
                low double precision,
                close double precision,
                volume double precision,
                vwap double precision,
                source text NOT NULL DEFAULT 'ib',
                created_at timestamptz DEFAULT now(),
                CONSTRAINT option_day_bar_uidx PRIMARY KEY (symbol, expiry, strike, option_right, bar_time, source)
            ) PARTITION BY RANGE (bar_time)
        """
        )
        cur.execute(
            "ALTER SEQUENCE option_day_option_day_id_seq OWNED BY option_day.option_day_id"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS option_day_symbol_expiry_strike_right_time ON option_day (symbol, expiry, strike, option_right, bar_time DESC)"
        )
        _log_table("option_min", "Option minute OHLC bars")
        cur.execute("CREATE SEQUENCE IF NOT EXISTS option_min_option_min_id_seq AS bigint")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS option_min (
                option_min_id bigint NOT NULL DEFAULT nextval('option_min_option_min_id_seq'),
                symbol text NOT NULL,
                expiry text NOT NULL,
                strike double precision NOT NULL,
                option_right text NOT NULL,
                period text NOT NULL,
                bar_time timestamptz NOT NULL,
                open double precision,
                high double precision,
                low double precision,
                close double precision,
                volume double precision,
                vwap double precision,
                source text NOT NULL DEFAULT 'ib',
                created_at timestamptz DEFAULT now(),
                CONSTRAINT option_min_bar_uidx PRIMARY KEY (symbol, expiry, strike, option_right, period, bar_time, source)
            ) PARTITION BY RANGE (bar_time)
        """
        )
        cur.execute(
            "ALTER SEQUENCE option_min_option_min_id_seq OWNED BY option_min.option_min_id"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS option_min_symbol_expiry_strike_right_period_time ON option_min (symbol, expiry, strike, option_right, period, bar_time DESC)"
        )
        _log("option_contracts, option_snapshots")
        _log_table("option_contracts", "Option contract definitions (contract_key)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS option_contracts (
                option_contracts_id bigserial PRIMARY KEY,
                contract_key text NOT NULL UNIQUE,
                symbol text NOT NULL,
                expiry text NOT NULL,
                strike double precision NOT NULL,
                option_right text NOT NULL,
                massive_option_ticker text,
                exercise_style text,
                shares_per_contract integer,
                created_at timestamptz DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS option_contracts_contract_key ON option_contracts (contract_key)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS option_contracts_symbol_expiry_strike_right ON option_contracts (symbol, expiry, strike, option_right)"
        )
        _log_table("option_expiration_cache", "Cached option expirations per underlying (Massive REST + TTL)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS option_expiration_cache (
                option_expiration_cache_id bigserial PRIMARY KEY,
                symbol text NOT NULL,
                expiry text NOT NULL,
                source text NOT NULL DEFAULT 'massive',
                last_seen_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now(),
                UNIQUE (symbol, expiry, source)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS option_expiration_cache_symbol_updated "
            "ON option_expiration_cache (symbol, updated_at DESC)"
        )
        _log_table("option_snapshots", "Option snapshot (point-in-time quote, RANGE partitioned by snapshot_ts)")
        cur.execute(
            """
            DO $snap_part$
            DECLARE
              tbl_kind char;
              m_start date;
              m_end date;
              part_name text;
            BEGIN
              -- Check if the table exists at all
              SELECT relkind INTO tbl_kind FROM pg_class
              WHERE relname = 'option_snapshots' AND relnamespace = 'public'::regnamespace;

              IF tbl_kind IS NULL THEN
                -- Fresh install: create as partitioned table directly
                CREATE SEQUENCE IF NOT EXISTS option_snapshots_option_snapshots_id_seq;
                CREATE TABLE option_snapshots (
                    option_snapshots_id bigint NOT NULL DEFAULT nextval('option_snapshots_option_snapshots_id_seq'),
                    contract_key text NOT NULL,
                    snapshot_ts timestamptz NOT NULL,
                    iv double precision,
                    delta double precision,
                    gamma double precision,
                    theta double precision,
                    vega double precision,
                    open_interest integer,
                    underlying_ticker text,
                    day_open double precision,
                    day_high double precision,
                    day_low double precision,
                    day_close double precision,
                    day_previous_close double precision,
                    day_change double precision,
                    day_change_percent double precision,
                    day_volume bigint,
                    day_vwap double precision,
                    day_last_updated timestamptz,
                    day_last_updated_day date GENERATED ALWAYS AS (
                      CASE WHEN day_last_updated IS NULL THEN NULL
                      ELSE (timezone('America/New_York', day_last_updated))::date END
                    ) STORED,
                    source text NOT NULL DEFAULT 'ib',
                    created_at timestamptz DEFAULT now(),
                    PRIMARY KEY (contract_key, snapshot_ts)
                ) PARTITION BY RANGE (snapshot_ts);
                ALTER SEQUENCE option_snapshots_option_snapshots_id_seq OWNED BY option_snapshots.option_snapshots_id;
                -- Create default partition for current month and next 3 months
                FOR i IN 0..3 LOOP
                  m_start := date_trunc('month', now())::date + (i || ' months')::interval;
                  m_end   := m_start + interval '1 month';
                  part_name := 'option_snapshots_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                  EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF option_snapshots FOR VALUES FROM (%L) TO (%L)',
                    part_name, m_start, m_end
                  );
                END LOOP;
                -- Default partition for anything outside defined ranges
                CREATE TABLE IF NOT EXISTS option_snapshots_default PARTITION OF option_snapshots DEFAULT;

              ELSIF tbl_kind = 'r' THEN
                -- Existing non-partitioned table: migrate to partitioned
                RAISE NOTICE 'Migrating option_snapshots from heap to RANGE partition on snapshot_ts ...';

                -- 0. Align legacy heap column set with current DDL (migrate_opt block runs later in _ensure_tables;
                -- without these, INSERT ... SELECT by name fails on older DBs that only had mid + created_at.)
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'iv'
                ) THEN
                  ALTER TABLE option_snapshots ADD COLUMN iv double precision;
                  ALTER TABLE option_snapshots ADD COLUMN delta double precision;
                  ALTER TABLE option_snapshots ADD COLUMN gamma double precision;
                  ALTER TABLE option_snapshots ADD COLUMN theta double precision;
                  ALTER TABLE option_snapshots ADD COLUMN vega double precision;
                  ALTER TABLE option_snapshots ADD COLUMN open_interest integer;
                  ALTER TABLE option_snapshots ADD COLUMN source text NOT NULL DEFAULT 'ib';
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'underlying_ticker'
                ) THEN
                  ALTER TABLE option_snapshots ADD COLUMN underlying_ticker text;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'day_open'
                ) THEN
                  ALTER TABLE option_snapshots ADD COLUMN day_open double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_high double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_low double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_close double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_previous_close double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_change double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_change_percent double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_volume bigint;
                  ALTER TABLE option_snapshots ADD COLUMN day_vwap double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_last_updated timestamptz;
                END IF;

                -- 1. Create new partitioned parent
                CREATE SEQUENCE IF NOT EXISTS option_snapshots_new_id_seq;
                CREATE TABLE option_snapshots_new (
                    option_snapshots_id bigint NOT NULL DEFAULT nextval('option_snapshots_new_id_seq'),
                    contract_key text NOT NULL,
                    snapshot_ts timestamptz NOT NULL,
                    iv double precision,
                    delta double precision,
                    gamma double precision,
                    theta double precision,
                    vega double precision,
                    open_interest integer,
                    underlying_ticker text,
                    day_open double precision,
                    day_high double precision,
                    day_low double precision,
                    day_close double precision,
                    day_previous_close double precision,
                    day_change double precision,
                    day_change_percent double precision,
                    day_volume bigint,
                    day_vwap double precision,
                    day_last_updated timestamptz,
                    day_last_updated_day date GENERATED ALWAYS AS (
                      CASE WHEN day_last_updated IS NULL THEN NULL
                      ELSE (timezone('America/New_York', day_last_updated))::date END
                    ) STORED,
                    source text NOT NULL DEFAULT 'ib',
                    created_at timestamptz DEFAULT now(),
                    PRIMARY KEY (contract_key, snapshot_ts)
                ) PARTITION BY RANGE (snapshot_ts);

                -- 2. Create monthly partitions covering existing data + future
                SELECT date_trunc('month', COALESCE(min(snapshot_ts), now()))::date INTO m_start
                FROM option_snapshots;
                m_end := (date_trunc('month', now()) + interval '4 months')::date;

                WHILE m_start < m_end LOOP
                  part_name := 'option_snapshots_new_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                  EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %I PARTITION OF option_snapshots_new FOR VALUES FROM (%L) TO (%L)',
                    part_name, m_start, (m_start + interval '1 month')::date
                  );
                  m_start := (m_start + interval '1 month')::date;
                END LOOP;
                CREATE TABLE IF NOT EXISTS option_snapshots_new_default PARTITION OF option_snapshots_new DEFAULT;

                -- 3. Copy data (must list columns by name: legacy heaps often had created_at before iv/greeks
                -- from ALTER TABLE ... ADD COLUMN, so SELECT * would misalign types.)
                INSERT INTO option_snapshots_new (
                    option_snapshots_id,
                    contract_key,
                    snapshot_ts,
                    iv,
                    delta,
                    gamma,
                    theta,
                    vega,
                    open_interest,
                    underlying_ticker,
                    day_open,
                    day_high,
                    day_low,
                    day_close,
                    day_previous_close,
                    day_change,
                    day_change_percent,
                    day_volume,
                    day_vwap,
                    day_last_updated,
                    source,
                    created_at
                )
                SELECT
                    option_snapshots_id,
                    contract_key,
                    snapshot_ts,
                    iv,
                    delta,
                    gamma,
                    theta,
                    vega,
                    open_interest,
                    underlying_ticker,
                    day_open,
                    day_high,
                    day_low,
                    day_close,
                    day_previous_close,
                    day_change,
                    day_change_percent,
                    day_volume,
                    day_vwap,
                    day_last_updated,
                    source,
                    created_at
                FROM option_snapshots;

                -- 4. Set sequence to continue after max id
                PERFORM setval('option_snapshots_new_id_seq',
                  GREATEST(
                    (SELECT COALESCE(max(option_snapshots_id), 0) FROM option_snapshots_new),
                    1
                  )
                );

                -- 5. Swap tables
                DROP MATERIALIZED VIEW IF EXISTS option_snapshots_latest;
                ALTER TABLE option_snapshots RENAME TO option_snapshots_old;
                ALTER TABLE option_snapshots_new RENAME TO option_snapshots;

                -- Rename partitions to standard naming
                FOR part_name IN
                  SELECT tablename FROM pg_tables
                  WHERE schemaname = 'public' AND tablename LIKE 'option_snapshots_new_%'
                LOOP
                  EXECUTE format('ALTER TABLE %I RENAME TO %I',
                    part_name, replace(part_name, '_new_', '_'));
                END LOOP;

                -- Rename and reassign sequence
                ALTER SEQUENCE option_snapshots_new_id_seq RENAME TO option_snapshots_option_snapshots_id_seq;
                ALTER SEQUENCE option_snapshots_option_snapshots_id_seq OWNED BY option_snapshots.option_snapshots_id;
                ALTER TABLE option_snapshots ALTER COLUMN option_snapshots_id SET DEFAULT nextval('option_snapshots_option_snapshots_id_seq');

                -- Drop old table (data already copied)
                DROP TABLE IF EXISTS option_snapshots_old;
                RAISE NOTICE 'Migration complete: option_snapshots is now RANGE partitioned.';

              ELSE
                -- Already partitioned (relkind = 'p'), ensure future partitions exist
                FOR i IN 0..3 LOOP
                  m_start := date_trunc('month', now())::date + (i || ' months')::interval;
                  m_end   := m_start + interval '1 month';
                  part_name := 'option_snapshots_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                  IF to_regclass('public.' || part_name) IS NULL THEN
                    EXECUTE format(
                      'CREATE TABLE %I PARTITION OF option_snapshots FOR VALUES FROM (%L) TO (%L)',
                      part_name, m_start, m_end
                    );
                  END IF;
                END LOOP;
              END IF;
            END $snap_part$;
            """
        )
        cur.execute(
            """
            DO $option_snapshots_pk$
            DECLARE
              dup_groups bigint;
              old_surrogate_pk boolean;
              pk_conname name;
              has_natural_pk boolean;
            BEGIN
              IF to_regclass('public.option_snapshots') IS NULL THEN
                RETURN;
              END IF;
              SELECT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid = 'public.option_snapshots'::regclass
                  AND c.contype = 'p'
                  AND pg_get_constraintdef(c.oid) LIKE '%option_snapshots_id%'
              ) INTO old_surrogate_pk;
              IF NOT old_surrogate_pk THEN
                RETURN;
              END IF;
              SELECT COUNT(*)::bigint INTO dup_groups FROM (
                SELECT 1 FROM option_snapshots
                GROUP BY contract_key, snapshot_ts
                HAVING COUNT(*) > 1
              ) d;
              IF dup_groups > 0 THEN
                RAISE NOTICE 'option_snapshots: duplicate (contract_key, snapshot_ts) groups exist — run scripts/db/dedupe_option_snapshots.py --apply then re-run schema refresh to migrate PRIMARY KEY to (contract_key, snapshot_ts).';
                RETURN;
              END IF;
              ALTER TABLE option_snapshots DROP CONSTRAINT IF EXISTS option_snapshots_contract_snapshot_uniq;
              SELECT c.conname INTO pk_conname
              FROM pg_constraint c
              WHERE c.conrelid = 'public.option_snapshots'::regclass
                AND c.contype = 'p'
                AND pg_get_constraintdef(c.oid) LIKE '%option_snapshots_id%'
              LIMIT 1;
              IF pk_conname IS NOT NULL THEN
                EXECUTE format('ALTER TABLE option_snapshots DROP CONSTRAINT %I', pk_conname);
              END IF;
              SELECT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid = 'public.option_snapshots'::regclass
                  AND c.contype = 'p'
                  AND pg_get_constraintdef(c.oid) LIKE '%contract_key%'
                  AND pg_get_constraintdef(c.oid) LIKE '%snapshot_ts%'
                  AND pg_get_constraintdef(c.oid) NOT LIKE '%option_snapshots_id%'
              ) INTO has_natural_pk;
              IF NOT has_natural_pk THEN
                ALTER TABLE option_snapshots ADD CONSTRAINT option_snapshots_pkey PRIMARY KEY (contract_key, snapshot_ts);
              END IF;
            END $option_snapshots_pk$;
            """
        )
        cur.execute("DROP INDEX IF EXISTS option_snapshots_contract_key_ts")

        _log("option_snapshots_latest materialized view (created after migrate_opt when base columns exist)")
        _log("migrate option_* tables for Massive (R-A6): source column, snapshots greeks")
        cur.execute(
            """
            DO $migrate_opt$
            BEGIN
              IF to_regclass('public.option_day') IS NOT NULL THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_day' AND column_name = 'source'
                ) THEN
                  ALTER TABLE option_day ADD COLUMN source text NOT NULL DEFAULT 'ib';
                END IF;
                IF EXISTS (
                  SELECT 1 FROM pg_constraint WHERE conname = 'option_day_symbol_expiry_strike_option_right_bar_time_key'
                ) THEN
                  ALTER TABLE option_day DROP CONSTRAINT option_day_symbol_expiry_strike_option_right_bar_time_key;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM pg_constraint WHERE conname = 'option_day_bar_uidx'
                ) THEN
                  ALTER TABLE option_day ADD CONSTRAINT option_day_bar_uidx
                    UNIQUE (symbol, expiry, strike, option_right, bar_time, source);
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_day' AND column_name = 'vwap'
                ) THEN
                  ALTER TABLE option_day ADD COLUMN vwap double precision;
                END IF;
              END IF;

              IF to_regclass('public.option_min') IS NOT NULL THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_min' AND column_name = 'source'
                ) THEN
                  ALTER TABLE option_min ADD COLUMN source text NOT NULL DEFAULT 'ib';
                END IF;
                IF EXISTS (
                  SELECT 1 FROM pg_constraint WHERE conname = 'option_min_symbol_expiry_strike_option_right_period_bar_time_key'
                ) THEN
                  ALTER TABLE option_min DROP CONSTRAINT option_min_symbol_expiry_strike_option_right_period_bar_time_key;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM pg_constraint WHERE conname = 'option_min_bar_uidx'
                ) THEN
                  ALTER TABLE option_min ADD CONSTRAINT option_min_bar_uidx
                    UNIQUE (symbol, expiry, strike, option_right, period, bar_time, source);
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_min' AND column_name = 'vwap'
                ) THEN
                  ALTER TABLE option_min ADD COLUMN vwap double precision;
                END IF;
              END IF;

              IF to_regclass('public.option_contracts') IS NOT NULL THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_contracts' AND column_name = 'massive_option_ticker'
                ) THEN
                  ALTER TABLE option_contracts ADD COLUMN massive_option_ticker text;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_contracts' AND column_name = 'exercise_style'
                ) THEN
                  ALTER TABLE option_contracts ADD COLUMN exercise_style text;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_contracts' AND column_name = 'shares_per_contract'
                ) THEN
                  ALTER TABLE option_contracts ADD COLUMN shares_per_contract integer;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_contracts' AND column_name = 'cfi'
                ) THEN
                  ALTER TABLE option_contracts DROP COLUMN cfi;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_contracts' AND column_name = 'primary_exchange'
                ) THEN
                  ALTER TABLE option_contracts DROP COLUMN primary_exchange;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_contracts' AND column_name = 'updated_at'
                ) THEN
                  ALTER TABLE option_contracts DROP COLUMN updated_at;
                END IF;
              END IF;

              IF to_regclass('public.option_snapshots') IS NOT NULL THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'iv'
                ) THEN
                  ALTER TABLE option_snapshots ADD COLUMN iv double precision;
                  ALTER TABLE option_snapshots ADD COLUMN delta double precision;
                  ALTER TABLE option_snapshots ADD COLUMN gamma double precision;
                  ALTER TABLE option_snapshots ADD COLUMN theta double precision;
                  ALTER TABLE option_snapshots ADD COLUMN vega double precision;
                  ALTER TABLE option_snapshots ADD COLUMN open_interest integer;
                  ALTER TABLE option_snapshots ADD COLUMN source text NOT NULL DEFAULT 'ib';
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'underlying_ticker'
                ) THEN
                  ALTER TABLE option_snapshots ADD COLUMN underlying_ticker text;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'day_open'
                ) THEN
                  ALTER TABLE option_snapshots ADD COLUMN day_open double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_high double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_low double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_close double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_previous_close double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_change double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_change_percent double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_volume bigint;
                  ALTER TABLE option_snapshots ADD COLUMN day_vwap double precision;
                  ALTER TABLE option_snapshots ADD COLUMN day_last_updated timestamptz;
                END IF;
              END IF;

              -- option_snapshots: generated day_last_updated_day, drop legacy quote/FMV columns, view + MV
              IF to_regclass('public.option_snapshots') IS NOT NULL
                 AND to_regclass('public.stock_day') IS NOT NULL THEN
                IF EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'last'
                  )
                  OR NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'option_snapshots'
                      AND column_name = 'day_last_updated_day'
                  )
                  OR NOT EXISTS (
                    SELECT 1 FROM pg_matviews
                    WHERE schemaname = 'public' AND matviewname = 'option_snapshots_latest'
                  )
                  OR EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'option_snapshots_latest' AND column_name = 'last'
                  )
                  OR NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'option_snapshots_latest'
                      AND column_name = 'day_last_updated_day'
                  ) THEN
                  DROP MATERIALIZED VIEW IF EXISTS option_snapshots_latest;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots'
                    AND column_name = 'day_last_updated_day'
                ) THEN
                  ALTER TABLE option_snapshots ADD COLUMN day_last_updated_day date
                    GENERATED ALWAYS AS (
                      CASE WHEN day_last_updated IS NULL THEN NULL
                      ELSE (timezone('America/New_York', day_last_updated))::date END
                    ) STORED;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'last'
                ) THEN
                  ALTER TABLE option_snapshots DROP COLUMN last;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'bid'
                ) THEN
                  ALTER TABLE option_snapshots DROP COLUMN bid;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'ask'
                ) THEN
                  ALTER TABLE option_snapshots DROP COLUMN ask;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'mid'
                ) THEN
                  ALTER TABLE option_snapshots DROP COLUMN mid;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'underlying_price'
                ) THEN
                  ALTER TABLE option_snapshots DROP COLUMN underlying_price;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'break_even_price'
                ) THEN
                  ALTER TABLE option_snapshots DROP COLUMN break_even_price;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'fmv'
                ) THEN
                  ALTER TABLE option_snapshots DROP COLUMN fmv;
                END IF;
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'option_snapshots' AND column_name = 'fmv_last_updated'
                ) THEN
                  ALTER TABLE option_snapshots DROP COLUMN fmv_last_updated;
                END IF;
                EXECUTE $v_os_ud$
                  CREATE OR REPLACE VIEW option_snapshots_with_underlying_day AS
                  SELECT
                    os.option_snapshots_id,
                    os.contract_key,
                    os.snapshot_ts,
                    os.iv,
                    os.delta,
                    os.gamma,
                    os.theta,
                    os.vega,
                    os.open_interest,
                    os.underlying_ticker,
                    os.day_open,
                    os.day_high,
                    os.day_low,
                    os.day_close,
                    os.day_previous_close,
                    os.day_change,
                    os.day_change_percent,
                    os.day_volume,
                    os.day_vwap,
                    os.day_last_updated,
                    os.day_last_updated_day,
                    os.source,
                    os.created_at,
                    sd.open AS u_open,
                    sd.high AS u_high,
                    sd.low AS u_low,
                    sd.close AS underlying_price,
                    sd.volume AS u_volume,
                    sd.vwap AS u_vwap
                  FROM public.option_snapshots os
                  LEFT JOIN public.stock_day sd
                    ON sd.source = 'massive'
                   AND sd.symbol = upper(trim(os.underlying_ticker))
                   AND sd.bar_time = os.day_last_updated_day
                $v_os_ud$;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_matviews
                    WHERE schemaname = 'public' AND matviewname = 'option_snapshots_latest'
                  ) THEN
                  EXECUTE $mvos$
                    CREATE MATERIALIZED VIEW option_snapshots_latest AS
                    SELECT DISTINCT ON (contract_key)
                      contract_key, snapshot_ts,
                      iv, delta, gamma, theta, vega, open_interest,
                      underlying_ticker,
                      day_open, day_high, day_low, day_close,
                      day_previous_close, day_change, day_change_percent,
                      day_volume, day_vwap, day_last_updated,
                      day_last_updated_day,
                      source, created_at
                    FROM option_snapshots
                    ORDER BY contract_key, snapshot_ts DESC
                  $mvos$;
                  CREATE UNIQUE INDEX IF NOT EXISTS option_snapshots_latest_ck
                    ON option_snapshots_latest (contract_key);
                END IF;
                CREATE INDEX IF NOT EXISTS option_snapshots_underlying_ticker_day
                  ON option_snapshots (underlying_ticker, day_last_updated_day);
              END IF;
            END
            $migrate_opt$;
            """
        )
        cur.execute(
            """
            DO $option_day_part$
            DECLARE
              rk char;
              m_start date;
              m_end date;
              part_name text;
              r record;
              min_bt timestamptz;
              max_bt timestamptz;
              mx bigint;
            BEGIN
              SELECT c.relkind INTO rk FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'public' AND c.relname = 'option_day';
              IF rk IS NULL THEN
                RETURN;
              END IF;

              IF rk = 'p' THEN
                SELECT date_trunc('month', COALESCE((SELECT MIN(bar_time) FROM public.option_day), now()))::date
                  INTO m_start;
                max_bt := COALESCE((SELECT MAX(bar_time) FROM public.option_day), now());
                m_end := (date_trunc('month', GREATEST(max_bt, now()))::date + interval '4 months')::date;
                WHILE m_start < m_end LOOP
                  part_name := 'option_day_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                  IF to_regclass('public.' || part_name) IS NULL THEN
                    EXECUTE format(
                      'CREATE TABLE %I PARTITION OF public.option_day FOR VALUES FROM (%L) TO (%L)',
                      part_name, m_start, (m_start + interval '1 month')::date
                    );
                  END IF;
                  m_start := (m_start + interval '1 month')::date;
                END LOOP;
                IF to_regclass('public.option_day_default') IS NULL THEN
                  CREATE TABLE option_day_default PARTITION OF public.option_day DEFAULT;
                END IF;
                RETURN;
              END IF;

              RAISE NOTICE 'Migrating option_day heap to RANGE partitions on bar_time ...';

              DELETE FROM public.option_day od
              WHERE EXISTS (
                SELECT 1 FROM public.option_day od2
                WHERE od2.symbol = od.symbol
                  AND od2.expiry = od.expiry
                  AND od2.strike = od.strike
                  AND od2.option_right = od.option_right
                  AND od2.bar_time = od.bar_time
                  AND od2.source = od.source
                  AND od2.option_day_id < od.option_day_id
              );

              CREATE TABLE option_day_new (
                option_day_id bigint NOT NULL DEFAULT nextval('option_day_option_day_id_seq'),
                symbol text NOT NULL,
                expiry text NOT NULL,
                strike double precision NOT NULL,
                option_right text NOT NULL,
                bar_time timestamptz NOT NULL,
                open double precision,
                high double precision,
                low double precision,
                close double precision,
                volume double precision,
                vwap double precision,
                source text NOT NULL DEFAULT 'ib',
                created_at timestamptz DEFAULT now(),
                CONSTRAINT option_day_mig_pkey PRIMARY KEY (symbol, expiry, strike, option_right, bar_time, source)
              ) PARTITION BY RANGE (bar_time);

              SELECT MIN(bar_time), MAX(bar_time) INTO min_bt, max_bt FROM public.option_day;
              m_start := date_trunc('month', COALESCE(min_bt, now()))::date;
              IF max_bt IS NULL THEN
                max_bt := COALESCE(min_bt, now());
              END IF;
              m_end := (date_trunc('month', GREATEST(max_bt, now()))::date + interval '4 months')::date;
              WHILE m_start < m_end LOOP
                part_name := 'option_day_new_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                EXECUTE format(
                  'CREATE TABLE %I PARTITION OF option_day_new FOR VALUES FROM (%L) TO (%L)',
                  part_name, m_start, (m_start + interval '1 month')::date
                );
                m_start := (m_start + interval '1 month')::date;
              END LOOP;
              CREATE TABLE option_day_new_default PARTITION OF option_day_new DEFAULT;

              INSERT INTO option_day_new (
                option_day_id, symbol, expiry, strike, option_right, bar_time,
                open, high, low, close, volume, vwap, source, created_at
              )
              SELECT
                option_day_id, symbol, expiry, strike, option_right, bar_time,
                open, high, low, close, volume, vwap, source, created_at
              FROM public.option_day;

              SELECT COALESCE(MAX(option_day_id), 1) INTO mx FROM option_day_new;
              PERFORM setval('option_day_option_day_id_seq', GREATEST(mx, 1));

              ALTER SEQUENCE option_day_option_day_id_seq OWNED BY NONE;
              DROP TABLE public.option_day;
              ALTER TABLE option_day_new RENAME TO option_day;
              ALTER SEQUENCE option_day_option_day_id_seq OWNED BY option_day.option_day_id;
              ALTER TABLE public.option_day
                ALTER COLUMN option_day_id SET DEFAULT nextval('option_day_option_day_id_seq');

              FOR r IN
                SELECT c.relname AS tname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname LIKE 'option_day_new_y%'
              LOOP
                EXECUTE format(
                  'ALTER TABLE %I RENAME TO %I',
                  r.tname,
                  replace(r.tname, 'option_day_new_', 'option_day_')
                );
              END LOOP;
              IF to_regclass('public.option_day_new_default') IS NOT NULL THEN
                ALTER TABLE option_day_new_default RENAME TO option_day_default;
              END IF;

              ALTER TABLE public.option_day RENAME CONSTRAINT option_day_mig_pkey TO option_day_bar_uidx;

              CREATE INDEX IF NOT EXISTS option_day_symbol_expiry_strike_right_time
                ON public.option_day (symbol, expiry, strike, option_right, bar_time DESC);
              RAISE NOTICE 'option_day: RANGE partition migration complete.';
            END
            $option_day_part$;
            """
        )
        cur.execute(
            """
            DO $option_min_part$
            DECLARE
              rk char;
              m_start date;
              m_end date;
              part_name text;
              r record;
              min_bt timestamptz;
              max_bt timestamptz;
              mx bigint;
            BEGIN
              SELECT c.relkind INTO rk FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'public' AND c.relname = 'option_min';
              IF rk IS NULL THEN
                RETURN;
              END IF;

              IF rk = 'p' THEN
                SELECT date_trunc('month', COALESCE((SELECT MIN(bar_time) FROM public.option_min), now()))::date
                  INTO m_start;
                max_bt := COALESCE((SELECT MAX(bar_time) FROM public.option_min), now());
                m_end := (date_trunc('month', GREATEST(max_bt, now()))::date + interval '4 months')::date;
                WHILE m_start < m_end LOOP
                  part_name := 'option_min_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                  IF to_regclass('public.' || part_name) IS NULL THEN
                    EXECUTE format(
                      'CREATE TABLE %I PARTITION OF public.option_min FOR VALUES FROM (%L) TO (%L)',
                      part_name, m_start, (m_start + interval '1 month')::date
                    );
                  END IF;
                  m_start := (m_start + interval '1 month')::date;
                END LOOP;
                IF to_regclass('public.option_min_default') IS NULL THEN
                  CREATE TABLE option_min_default PARTITION OF public.option_min DEFAULT;
                END IF;
                RETURN;
              END IF;

              RAISE NOTICE 'Migrating option_min heap to RANGE partitions on bar_time ...';

              DELETE FROM public.option_min om
              WHERE EXISTS (
                SELECT 1 FROM public.option_min om2
                WHERE om2.symbol = om.symbol
                  AND om2.expiry = om.expiry
                  AND om2.strike = om.strike
                  AND om2.option_right = om.option_right
                  AND om2.period = om.period
                  AND om2.bar_time = om.bar_time
                  AND om2.source = om.source
                  AND om2.option_min_id < om.option_min_id
              );

              CREATE TABLE option_min_new (
                option_min_id bigint NOT NULL DEFAULT nextval('option_min_option_min_id_seq'),
                symbol text NOT NULL,
                expiry text NOT NULL,
                strike double precision NOT NULL,
                option_right text NOT NULL,
                period text NOT NULL,
                bar_time timestamptz NOT NULL,
                open double precision,
                high double precision,
                low double precision,
                close double precision,
                volume double precision,
                vwap double precision,
                source text NOT NULL DEFAULT 'ib',
                created_at timestamptz DEFAULT now(),
                CONSTRAINT option_min_mig_pkey PRIMARY KEY (symbol, expiry, strike, option_right, period, bar_time, source)
              ) PARTITION BY RANGE (bar_time);

              SELECT MIN(bar_time), MAX(bar_time) INTO min_bt, max_bt FROM public.option_min;
              m_start := date_trunc('month', COALESCE(min_bt, now()))::date;
              IF max_bt IS NULL THEN
                max_bt := COALESCE(min_bt, now());
              END IF;
              m_end := (date_trunc('month', GREATEST(max_bt, now()))::date + interval '4 months')::date;
              WHILE m_start < m_end LOOP
                part_name := 'option_min_new_y' || to_char(m_start, 'YYYY') || 'm' || to_char(m_start, 'MM');
                EXECUTE format(
                  'CREATE TABLE %I PARTITION OF option_min_new FOR VALUES FROM (%L) TO (%L)',
                  part_name, m_start, (m_start + interval '1 month')::date
                );
                m_start := (m_start + interval '1 month')::date;
              END LOOP;
              CREATE TABLE option_min_new_default PARTITION OF option_min_new DEFAULT;

              INSERT INTO option_min_new (
                option_min_id, symbol, expiry, strike, option_right, period, bar_time,
                open, high, low, close, volume, vwap, source, created_at
              )
              SELECT
                option_min_id, symbol, expiry, strike, option_right, period, bar_time,
                open, high, low, close, volume, vwap, source, created_at
              FROM public.option_min;

              SELECT COALESCE(MAX(option_min_id), 1) INTO mx FROM option_min_new;
              PERFORM setval('option_min_option_min_id_seq', GREATEST(mx, 1));

              ALTER SEQUENCE option_min_option_min_id_seq OWNED BY NONE;
              DROP TABLE public.option_min;
              ALTER TABLE option_min_new RENAME TO option_min;
              ALTER SEQUENCE option_min_option_min_id_seq OWNED BY option_min.option_min_id;
              ALTER TABLE public.option_min
                ALTER COLUMN option_min_id SET DEFAULT nextval('option_min_option_min_id_seq');

              FOR r IN
                SELECT c.relname AS tname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public' AND c.relname LIKE 'option_min_new_y%'
              LOOP
                EXECUTE format(
                  'ALTER TABLE %I RENAME TO %I',
                  r.tname,
                  replace(r.tname, 'option_min_new_', 'option_min_')
                );
              END LOOP;
              IF to_regclass('public.option_min_new_default') IS NOT NULL THEN
                ALTER TABLE option_min_new_default RENAME TO option_min_default;
              END IF;

              ALTER TABLE public.option_min RENAME CONSTRAINT option_min_mig_pkey TO option_min_bar_uidx;

              CREATE INDEX IF NOT EXISTS option_min_symbol_expiry_strike_right_period_time
                ON public.option_min (symbol, expiry, strike, option_right, period, bar_time DESC);
              RAISE NOTICE 'option_min: RANGE partition migration complete.';
            END
            $option_min_part$;
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

        _log("job_massive_backfill, option_open_interest_daily, option_trades, massive_corporate_action (R-A6)")
        _log_table("job_massive_backfill", "Massive async sync job queue")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS job_massive_backfill (
                job_massive_backfill_id bigserial PRIMARY KEY,
                kind text NOT NULL,
                payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                status text NOT NULL DEFAULT 'pending',
                result jsonb,
                celery_task_id text,
                created_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS job_massive_backfill_status_created ON job_massive_backfill (status, created_at)"
        )
        cur.execute(
            """
            DO $jmb_hash$
            BEGIN
              IF to_regclass('public.job_massive_backfill') IS NOT NULL THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'job_massive_backfill' AND column_name = 'payload_hash'
                ) THEN
                  ALTER TABLE job_massive_backfill ADD COLUMN payload_hash text;
                END IF;
              END IF;
            END $jmb_hash$;
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS job_massive_backfill_dedup
            ON job_massive_backfill (kind, payload_hash)
            WHERE status IN ('pending', 'running') AND payload_hash IS NOT NULL
            """
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
                tickers_id bigint NULL REFERENCES public.tickers (tickers_id) ON DELETE SET NULL,
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

        # --- Massive Stocks Fundamentals v1 (flat REST) — SEPA Data Ready Steps 4–9 ---
        _log_table(
            "stock_income_statements",
            "Massive GET /stocks/financials/v1/income-statements (quarterly/annual/ttm)",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.stock_income_statements (
                symbol text NOT NULL,
                timeframe text NOT NULL,
                period_end date NOT NULL,
                filing_date date NULL,
                fiscal_year integer NOT NULL,
                fiscal_quarter integer NOT NULL DEFAULT 0,
                basic_earnings_per_share double precision NULL,
                diluted_earnings_per_share double precision NULL,
                revenue double precision NULL,
                basic_shares_outstanding double precision NULL,
                diluted_shares_outstanding double precision NULL,
                consolidated_net_income_loss double precision NULL,
                cost_of_revenue double precision NULL,
                gross_profit double precision NULL,
                operating_income double precision NULL,
                total_operating_expenses double precision NULL,
                selling_general_administrative double precision NULL,
                research_development double precision NULL,
                depreciation_depletion_amortization double precision NULL,
                ebitda double precision NULL,
                interest_income double precision NULL,
                interest_expense double precision NULL,
                other_income_expense double precision NULL,
                total_other_income_expense double precision NULL,
                income_before_income_taxes double precision NULL,
                income_taxes double precision NULL,
                net_income_loss_attributable_common_shareholders double precision NULL,
                noncontrolling_interest double precision NULL,
                discontinued_operations double precision NULL,
                extraordinary_items double precision NULL,
                equity_in_affiliates double precision NULL,
                preferred_stock_dividends_declared double precision NULL,
                other_operating_expenses double precision NULL,
                tickers jsonb NULL,
                cik text NULL,
                source text NOT NULL DEFAULT 'massive',
                fetched_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, timeframe, period_end, source)
            )
            """
        )
        cur.execute(
            """
            ALTER TABLE public.stock_income_statements
            ADD COLUMN IF NOT EXISTS tickers jsonb NULL
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_stock_income_sym_tf
            ON public.stock_income_statements (symbol, timeframe, source)
            """
        )

        _log_table(
            "stock_balance_sheets",
            "Massive GET /stocks/financials/v1/balance-sheets",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.stock_balance_sheets (
                symbol text NOT NULL,
                timeframe text NOT NULL,
                period_end date NOT NULL,
                filing_date date NULL,
                fiscal_year integer NOT NULL,
                fiscal_quarter integer NOT NULL DEFAULT 0,
                accounts_payable double precision NULL,
                accrued_and_other_current_liabilities double precision NULL,
                accumulated_other_comprehensive_income double precision NULL,
                additional_paid_in_capital double precision NULL,
                cash_and_equivalents double precision NULL,
                cik text NULL,
                commitments_and_contingencies double precision NULL,
                common_stock double precision NULL,
                debt_current double precision NULL,
                deferred_revenue_current double precision NULL,
                goodwill double precision NULL,
                intangible_assets_net double precision NULL,
                inventories double precision NULL,
                long_term_debt_and_capital_lease_obligations double precision NULL,
                noncontrolling_interest double precision NULL,
                other_assets double precision NULL,
                other_current_assets double precision NULL,
                other_equity double precision NULL,
                other_noncurrent_liabilities double precision NULL,
                preferred_stock double precision NULL,
                property_plant_equipment_net double precision NULL,
                receivables double precision NULL,
                retained_earnings_deficit double precision NULL,
                short_term_investments double precision NULL,
                total_assets double precision NULL,
                total_current_assets double precision NULL,
                total_current_liabilities double precision NULL,
                total_equity double precision NULL,
                total_equity_attributable_to_parent double precision NULL,
                total_liabilities double precision NULL,
                total_liabilities_and_equity double precision NULL,
                treasury_stock double precision NULL,
                source text NOT NULL DEFAULT 'massive',
                fetched_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, timeframe, period_end, source)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_stock_balance_sym_tf
            ON public.stock_balance_sheets (symbol, timeframe, source)
            """
        )

        _log_table(
            "stock_cash_flows",
            "Massive GET /stocks/financials/v1/cash-flow-statements",
        )
        # Column names match Massive results[] (flat floats). Drop mismatched layouts so INSERT stays aligned.
        cur.execute(
            """
            DO $stock_cf_migrate$
            BEGIN
              IF to_regclass('public.stock_cash_flows') IS NOT NULL THEN
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_cash_flows'
                    AND column_name IN (
                      'net_cash_flow_from_operating_activities',
                      'net_cash_flow_from_investing_activities',
                      'net_cash_flow_from_financing_activities',
                      'net_change_in_cash_and_equivalents',
                      'capital_expenditure',
                      'free_cash_flow',
                      'depreciation_and_amortization'
                    )
                )
                OR NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_cash_flows'
                    AND column_name = 'change_in_cash_and_equivalents'
                )
                OR NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_cash_flows'
                    AND column_name = 'purchase_of_property_plant_and_equipment'
                )
                THEN
                  DROP TABLE public.stock_cash_flows CASCADE;
                END IF;
              END IF;
            END
            $stock_cf_migrate$;
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.stock_cash_flows (
                symbol text NOT NULL,
                timeframe text NOT NULL,
                period_end date NOT NULL,
                filing_date date NULL,
                fiscal_year integer NOT NULL,
                fiscal_quarter integer NOT NULL DEFAULT 0,
                cik text NULL,
                cash_from_operating_activities_continuing_operations double precision NULL,
                change_in_cash_and_equivalents double precision NULL,
                change_in_other_operating_assets_and_liabilities_net double precision NULL,
                depreciation_depletion_and_amortization double precision NULL,
                dividends double precision NULL,
                effect_of_currency_exchange_rate double precision NULL,
                income_loss_from_discontinued_operations double precision NULL,
                long_term_debt_issuances_repayments double precision NULL,
                net_cash_from_financing_activities double precision NULL,
                net_cash_from_financing_activities_continuing_operations double precision NULL,
                net_cash_from_financing_activities_discontinued_operations double precision NULL,
                net_cash_from_investing_activities double precision NULL,
                net_cash_from_investing_activities_continuing_operations double precision NULL,
                net_cash_from_investing_activities_discontinued_operations double precision NULL,
                net_cash_from_operating_activities double precision NULL,
                net_cash_from_operating_activities_discontinued_operations double precision NULL,
                net_income double precision NULL,
                noncontrolling_interests double precision NULL,
                other_cash_adjustments double precision NULL,
                other_financing_activities double precision NULL,
                other_investing_activities double precision NULL,
                other_operating_activities double precision NULL,
                purchase_of_property_plant_and_equipment double precision NULL,
                sale_of_property_plant_and_equipment double precision NULL,
                short_term_debt_issuances_repayments double precision NULL,
                source text NOT NULL DEFAULT 'massive',
                fetched_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, timeframe, period_end, source)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_stock_cashflow_sym_tf
            ON public.stock_cash_flows (symbol, timeframe, source)
            """
        )

        _log_table(
            "stock_ratios",
            "Massive GET /stocks/financials/v1/ratios (TTM ratios per trading date)",
        )
        cur.execute(
            """
            DO $stock_ratios_migrate$
            BEGIN
              IF to_regclass('public.stock_ratios') IS NOT NULL THEN
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_ratios'
                    AND column_name IN ('timeframe', 'period_end', 'current_ratio', 'gross_margin')
                )
                OR NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_ratios'
                    AND column_name = 'average_volume'
                )
                THEN
                  DROP TABLE public.stock_ratios CASCADE;
                END IF;
              END IF;
            END
            $stock_ratios_migrate$;
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.stock_ratios (
                symbol text NOT NULL,
                date date NOT NULL,
                average_volume double precision NULL,
                cash double precision NULL,
                cik text NULL,
                "current" double precision NULL,
                debt_to_equity double precision NULL,
                dividend_yield double precision NULL,
                earnings_per_share double precision NULL,
                enterprise_value double precision NULL,
                ev_to_ebitda double precision NULL,
                ev_to_sales double precision NULL,
                free_cash_flow double precision NULL,
                market_cap double precision NULL,
                price double precision NULL,
                price_to_book double precision NULL,
                price_to_cash_flow double precision NULL,
                price_to_earnings double precision NULL,
                price_to_free_cash_flow double precision NULL,
                price_to_sales double precision NULL,
                quick double precision NULL,
                return_on_assets double precision NULL,
                return_on_equity double precision NULL,
                source text NOT NULL DEFAULT 'massive',
                fetched_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, date, source)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_stock_ratios_sym_date_src
            ON public.stock_ratios (symbol, source, date DESC)
            """
        )

        _log_table(
            "stock_short_interest",
            "Massive GET /stocks/v1/short-interest",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.stock_short_interest (
                symbol text NOT NULL,
                settlement_date date NOT NULL,
                short_interest bigint NULL,
                avg_daily_volume bigint NULL,
                days_to_cover double precision NULL,
                cik text NULL,
                source text NOT NULL DEFAULT 'massive',
                fetched_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, settlement_date, source)
            )
            """
        )
        cur.execute(
            """
            DO $si_adv_mig$
            BEGIN
              IF to_regclass('public.stock_short_interest') IS NOT NULL THEN
                IF EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'stock_short_interest'
                    AND column_name = 'avg_daily_volume' AND udt_name = 'float8'
                ) THEN
                  ALTER TABLE public.stock_short_interest
                    ALTER COLUMN avg_daily_volume TYPE bigint
                    USING CASE
                      WHEN avg_daily_volume IS NULL THEN NULL
                      ELSE round(avg_daily_volume)::bigint
                    END;
                END IF;
              END IF;
            END
            $si_adv_mig$;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_stock_short_int_sym_settle
            ON public.stock_short_interest (symbol, settlement_date DESC)
            """
        )

        _log_table(
            "stock_short_volume",
            "Massive GET /stocks/v1/short-volume",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.stock_short_volume (
                symbol text NOT NULL,
                trade_date date NOT NULL,
                adf_short_volume bigint NULL,
                adf_short_volume_exempt bigint NULL,
                exempt_volume double precision NULL,
                nasdaq_carteret_short_volume bigint NULL,
                nasdaq_carteret_short_volume_exempt bigint NULL,
                nasdaq_chicago_short_volume bigint NULL,
                nasdaq_chicago_short_volume_exempt bigint NULL,
                non_exempt_volume double precision NULL,
                nyse_short_volume bigint NULL,
                nyse_short_volume_exempt bigint NULL,
                short_volume bigint NULL,
                short_volume_ratio double precision NULL,
                total_volume bigint NULL,
                exchanges jsonb NULL,
                cik text NULL,
                source text NOT NULL DEFAULT 'massive',
                fetched_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (symbol, trade_date, source)
            )
            """
        )
        cur.execute(
            """
            DO $sv_cols_mig$
            BEGIN
              IF to_regclass('public.stock_short_volume') IS NULL THEN
                RETURN;
              END IF;
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'stock_short_volume'
                  AND column_name = 'adf_short_volume'
              ) THEN
                ALTER TABLE public.stock_short_volume
                  ADD COLUMN adf_short_volume bigint NULL,
                  ADD COLUMN adf_short_volume_exempt bigint NULL,
                  ADD COLUMN exempt_volume double precision NULL,
                  ADD COLUMN nasdaq_carteret_short_volume bigint NULL,
                  ADD COLUMN nasdaq_carteret_short_volume_exempt bigint NULL,
                  ADD COLUMN nasdaq_chicago_short_volume bigint NULL,
                  ADD COLUMN nasdaq_chicago_short_volume_exempt bigint NULL,
                  ADD COLUMN non_exempt_volume double precision NULL,
                  ADD COLUMN nyse_short_volume bigint NULL,
                  ADD COLUMN nyse_short_volume_exempt bigint NULL;
              END IF;
            END
            $sv_cols_mig$;
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_stock_short_vol_sym_date
            ON public.stock_short_volume (symbol, trade_date DESC)
            """
        )

        _log_table("v_us_equity_universe", "View: US common-stock universe from tickers (active CS, locale=us, market=stocks)")
        cur.execute(
            """
            CREATE OR REPLACE VIEW public.v_us_equity_universe AS
            SELECT
                t.tickers_id,
                upper(trim(t.ticker)) AS symbol,
                t.name,
                t.market,
                t.locale,
                t.primary_exchange,
                t.instrument_type,
                t.active,
                t.delisted_utc,
                o.list_date,
                o.sector,
                o.industry
            FROM public.tickers t
            LEFT JOIN public.ticker_overview o ON o.tickers_id = t.tickers_id
            WHERE COALESCE(t.active, false) = true
              AND lower(COALESCE(t.locale, '')) = 'us'
              AND lower(COALESCE(t.market, '')) = 'stocks'
              AND lower(COALESCE(t.instrument_type, '')) = 'cs'
            """
        )
        # Keep the old sepa-prefixed name as a compatibility alias
        cur.execute(
            "CREATE OR REPLACE VIEW public.v_sepa_us_equity_universe AS SELECT * FROM public.v_us_equity_universe"
        )

        _log_table(
            "v_sepa_symbol_price_readiness",
            "View: per-symbol stock_day bar counts and price_ready vs lookback window (calendar days)",
        )
        cur.execute(
            """
            CREATE OR REPLACE VIEW public.v_sepa_symbol_price_readiness AS
            WITH params AS (
                SELECT
                    'massive'::text AS price_source,
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
                min(sd.bar_time)::date AS first_bar_date,
                max(sd.bar_time)::date AS last_bar_date,
                count(*) FILTER (WHERE sd.close IS NULL)::integer AS null_close_rows,
                count(*) FILTER (WHERE sd.volume IS NULL)::integer AS null_volume_rows,
                (
                    count(*) >= p.min_bar_rows
                    AND max(sd.bar_time) >= (
                        p.as_of_date - (p.max_stale_calendar_days || ' days')::interval
                    )::date
                    AND count(*) FILTER (WHERE sd.close IS NULL) = 0
                    AND count(*) FILTER (WHERE sd.volume IS NULL) = 0
                ) AS price_ready
            FROM params p
            JOIN public.stock_day sd
                ON sd.source = p.price_source
               AND sd.bar_time >= p.window_start
               AND sd.bar_time <= p.as_of_date
            GROUP BY p.as_of_date, p.price_source, p.min_bar_rows, p.max_stale_calendar_days, p.window_start,
                     upper(trim(sd.symbol))
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

        _log_table("report_option_max_pain_daily", "Max Pain daily report (R-A6)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS report_option_max_pain_daily (
                report_option_max_pain_daily_id bigserial PRIMARY KEY,
                symbol text NOT NULL,
                expiry text NOT NULL,
                trade_date date NOT NULL,
                max_pain_strike double precision NOT NULL,
                underlying_close double precision,
                total_oi integer,
                computation_detail jsonb,
                source text NOT NULL DEFAULT 'massive',
                created_at timestamptz DEFAULT now(),
                UNIQUE (symbol, expiry, trade_date, source)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS report_max_pain_symbol_date ON report_option_max_pain_daily (symbol, trade_date DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS report_max_pain_symbol_expiry_date ON report_option_max_pain_daily (symbol, expiry, trade_date DESC)"
        )

        _log_table(
            "report_option_atm_iv_daily",
            "Daily ATM IV rollup per symbol/expiry (Option Discovery IV cone fast path)",
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS report_option_atm_iv_daily (
                report_option_atm_iv_daily_id bigserial PRIMARY KEY,
                symbol text NOT NULL,
                expiry text NOT NULL,
                trade_date date NOT NULL,
                source text NOT NULL DEFAULT 'massive',
                atm_iv double precision,
                iv_call double precision,
                iv_put double precision,
                strike double precision,
                underlying_price double precision,
                created_at timestamptz DEFAULT now(),
                UNIQUE (symbol, expiry, trade_date, source)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS report_atm_iv_symbol_expiry_date ON report_option_atm_iv_daily (symbol, expiry, trade_date DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS report_atm_iv_symbol_date ON report_option_atm_iv_daily (symbol, trade_date DESC)"
        )

        _log_table("option_open_interest_daily", "Option daily open interest (Massive)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS option_open_interest_daily (
                option_open_interest_daily_id bigserial PRIMARY KEY,
                contract_key text NOT NULL,
                symbol text NOT NULL,
                expiry text NOT NULL,
                strike double precision NOT NULL,
                option_right text NOT NULL,
                trade_date date NOT NULL,
                open_interest integer NOT NULL,
                source text NOT NULL DEFAULT 'massive',
                created_at timestamptz DEFAULT now(),
                UNIQUE (contract_key, trade_date, source)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS option_oi_daily_contract_date ON option_open_interest_daily (contract_key, trade_date DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS option_oi_daily_symbol_date ON option_open_interest_daily (symbol, trade_date DESC)"
        )
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
        _log_table("massive_corporate_action", "Corporate actions cache (Massive)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS massive_corporate_action (
                massive_corporate_action_id bigserial PRIMARY KEY,
                symbol text NOT NULL,
                action_type text NOT NULL,
                ex_date date,
                record_date date,
                payment_date date,
                ratio_from double precision,
                ratio_to double precision,
                amount double precision,
                currency text,
                description text,
                source text NOT NULL DEFAULT 'massive',
                created_at timestamptz DEFAULT now(),
                UNIQUE (symbol, action_type, ex_date, source)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS massive_corp_action_symbol_ex ON massive_corporate_action (symbol, ex_date DESC)"
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
