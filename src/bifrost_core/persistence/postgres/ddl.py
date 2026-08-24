"""PostgreSQL DDL: current schema (CREATE TABLE IF NOT EXISTS + indexes only)."""

from __future__ import annotations

from datetime import date
from typing import Any

# IB / brokerage tables live in bifrost_golden_source.raw_broker.* (see brokerage_ddl.py).
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
_P7_RETIRED_PUBLIC_TABLES = frozenset(
    {
        "option_trades",
        "job_bars_backfill",
        "job_sepa_phase4",
        "job_ticker_reference_state",
        "stock_readiness_daily",
        "research_sepa_fundamentals_cache",
    }
)
# P8: data completeness / source-void moved to Market Data Plugin ops_jobs.data_source_void.
_P8_RETIRED_PUBLIC_TABLES = frozenset(
    {
        "preference_data_gap_ack",
        "preference_sepa_gap_ack",
    }
)
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


def _migrate_strategy_kv_to_jsonb(cur) -> None:
    """Fold strategy_template_param / characteristic / structure_meta into parent jsonb.

    Idempotent: ADD COLUMN IF NOT EXISTS, copy when child tables exist, then DROP.
    """
    cur.execute(
        """
        ALTER TABLE strategy_template
          ADD COLUMN IF NOT EXISTS params_json jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN IF NOT EXISTS characteristics_json jsonb NOT NULL DEFAULT '[]'::jsonb
        """
    )
    cur.execute(
        """
        ALTER TABLE strategy_structure
          ADD COLUMN IF NOT EXISTS meta_json jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )
    cur.execute("SELECT to_regclass('public.strategy_template_param')")
    if cur.fetchone()[0] is not None:
        cur.execute(
            """
            UPDATE strategy_template t SET params_json = COALESCE(agg.params, '[]'::jsonb)
            FROM (
              SELECT strategy_template_id,
                     jsonb_agg(
                       jsonb_build_object(
                         'meta_key', meta_key,
                         'display_label', display_label,
                         'default_value_text', default_value_text,
                         'param_kind', param_kind,
                         'sort_order', sort_order
                       )
                       ORDER BY sort_order, meta_key
                     ) AS params
              FROM strategy_template_param
              GROUP BY strategy_template_id
            ) agg
            WHERE t.strategy_template_id = agg.strategy_template_id
            """
        )
    cur.execute("SELECT to_regclass('public.strategy_template_characteristic')")
    if cur.fetchone()[0] is not None:
        cur.execute(
            """
            UPDATE strategy_template t SET characteristics_json = COALESCE(agg.chars, '[]'::jsonb)
            FROM (
              SELECT strategy_template_id,
                     jsonb_agg(characteristic_text ORDER BY sort_order) AS chars
              FROM strategy_template_characteristic
              GROUP BY strategy_template_id
            ) agg
            WHERE t.strategy_template_id = agg.strategy_template_id
            """
        )
    cur.execute("SELECT to_regclass('public.strategy_structure_meta')")
    if cur.fetchone()[0] is not None:
        cur.execute(
            """
            UPDATE strategy_structure s SET meta_json = COALESCE(agg.meta, '{}'::jsonb)
            FROM (
              SELECT strategy_structure_id,
                     jsonb_object_agg(meta_key, meta_value_text) AS meta
              FROM strategy_structure_meta
              WHERE meta_key IS NOT NULL AND meta_key <> ''
              GROUP BY strategy_structure_id
            ) agg
            WHERE s.strategy_structure_id = agg.strategy_structure_id
            """
        )
    cur.execute("DROP TABLE IF EXISTS strategy_template_param CASCADE")
    cur.execute("DROP TABLE IF EXISTS strategy_template_characteristic CASCADE")
    cur.execute("DROP TABLE IF EXISTS strategy_structure_meta CASCADE")


def _retire_strategy_history(cur) -> None:
    """Drop strategy_history (Wave 3). Idempotent: table was never written in production."""
    cur.execute("DROP TABLE IF EXISTS strategy_history CASCADE")


_OPS_AUDIT_LOG_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ops_audit_log (
    id          BIGSERIAL,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    operator    TEXT NOT NULL DEFAULT 'unknown',
    source_ip   TEXT,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    command_id  TEXT,
    outcome     TEXT NOT NULL,
    detail      TEXT,
    request_id  TEXT,
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp)
"""


def _ops_audit_partition_name(year: int, month: int) -> str:
    return f"ops_audit_log_y{year:04d}m{month:02d}"


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_months(d: date, months: int) -> date:
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    return date(y, m, 1)


def _ops_audit_log_is_partitioned(cur) -> bool:
    cur.execute(
        """
        SELECT c.relkind = 'p'
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'ops_audit_log'
        """
    )
    row = cur.fetchone()
    return bool(row and row[0])


def _ops_audit_log_timestamp_is_timestamptz(cur) -> bool:
    cur.execute(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'ops_audit_log'
          AND column_name = 'timestamp'
        """
    )
    row = cur.fetchone()
    return bool(row and row[0] == "timestamp with time zone")


def _ensure_ops_audit_log_partitions(cur, *, months_back: int = 3, months_forward: int = 2) -> None:
    """Create monthly partitions for ops_audit_log (current ± window) + DEFAULT."""
    if not _ops_audit_log_is_partitioned(cur):
        return
    today = date.today()
    start = _add_months(_month_start(today), -months_back)
    end = _add_months(_month_start(today), months_forward + 1)
    cur_m = start
    while cur_m < end:
        part = _ops_audit_partition_name(cur_m.year, cur_m.month)
        nxt = _add_months(cur_m, 1)
        cur.execute("SELECT to_regclass(%s)", (f"public.{part}",))
        if cur.fetchone()[0] is None:
            cur.execute(
                f"""
                CREATE TABLE {part} PARTITION OF ops_audit_log
                FOR VALUES FROM (%s) TO (%s)
                """,
                (cur_m.isoformat(), nxt.isoformat()),
            )
        cur_m = nxt
    cur.execute("SELECT to_regclass('public.ops_audit_log_default')")
    if cur.fetchone()[0] is None:
        cur.execute("CREATE TABLE ops_audit_log_default PARTITION OF ops_audit_log DEFAULT")


def _upgrade_ops_audit_log_to_partitioned(cur) -> None:
    """Migrate heap ops_audit_log (double epoch) → partitioned timestamptz.

    Idempotent: skip when already partitioned with timestamptz column.
    """
    cur.execute("SELECT to_regclass('public.ops_audit_log')")
    if cur.fetchone()[0] is None:
        cur.execute(_OPS_AUDIT_LOG_CREATE_SQL)
        _ensure_ops_audit_log_partitions(cur)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_ops_audit_log_ts ON ops_audit_log (timestamp DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_ops_audit_log_outcome ON ops_audit_log (outcome)"
        )
        return

    if _ops_audit_log_is_partitioned(cur) and _ops_audit_log_timestamp_is_timestamptz(cur):
        _ensure_ops_audit_log_partitions(cur)
        return

    # Leftover from a failed prior migration.
    cur.execute("DROP TABLE IF EXISTS ops_audit_log_legacy CASCADE")

    cur.execute("ALTER TABLE ops_audit_log RENAME TO ops_audit_log_legacy")
    # Drop indexes that followed the rename (name stays on legacy); recreate on parent.
    cur.execute(_OPS_AUDIT_LOG_CREATE_SQL)
    _ensure_ops_audit_log_partitions(cur)

    # Detect legacy column type: double precision epoch vs already timestamptz.
    cur.execute(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'ops_audit_log_legacy'
          AND column_name = 'timestamp'
        """
    )
    legacy_type = (cur.fetchone() or [None])[0]
    if legacy_type == "double precision":
        ts_expr = "to_timestamp(timestamp)"
    else:
        ts_expr = "timestamp::timestamptz"

    cur.execute(
        f"""
        INSERT INTO ops_audit_log (
            id, timestamp, operator, source_ip, action, target,
            command_id, outcome, detail, request_id
        )
        SELECT
            id, {ts_expr}, operator, source_ip, action, target,
            command_id, outcome, detail, request_id
        FROM ops_audit_log_legacy
        """
    )
    # Advance BIGSERIAL past migrated ids.
    cur.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('ops_audit_log', 'id'),
            COALESCE((SELECT MAX(id) FROM ops_audit_log), 1),
            true
        )
        """
    )
    cur.execute("DROP TABLE ops_audit_log_legacy CASCADE")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ops_audit_log_ts ON ops_audit_log (timestamp DESC)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_ops_audit_log_outcome ON ops_audit_log (outcome)"
    )


def drop_ops_audit_log_partitions_older_than(
    conn_or_cur: Any,
    *,
    cutoff_months: int = 3,
) -> int:
    """Drop monthly ops_audit_log partitions older than cutoff_months.

    Accepts a connection or cursor. Returns number of partitions dropped.
    Celery beat was retired; call from ensure_tables / CronJob / manual script.
    """
    owns_cur = False
    if hasattr(conn_or_cur, "cursor") and not hasattr(conn_or_cur, "execute"):
        cur = conn_or_cur.cursor()
        owns_cur = True
        conn = conn_or_cur
    else:
        cur = conn_or_cur
        conn = None

    try:
        if not _ops_audit_log_is_partitioned(cur):
            return 0
        cutoff = _add_months(_month_start(date.today()), -cutoff_months)
        cur.execute(
            """
            SELECT c.relname
            FROM pg_inherits i
            JOIN pg_class c ON c.oid = i.inhrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_class p ON p.oid = i.inhparent
            JOIN pg_namespace pn ON pn.oid = p.relnamespace
            WHERE pn.nspname = 'public'
              AND p.relname = 'ops_audit_log'
              AND n.nspname = 'public'
              AND c.relname ~ '^ops_audit_log_y[0-9]{4}m[0-9]{2}$'
            """
        )
        dropped = 0
        for (part_name,) in cur.fetchall():
            y = int(part_name[-7:-3])
            m = int(part_name[-2:])
            part_month = date(y, m, 1)
            if part_month < cutoff:
                cur.execute(f"DROP TABLE IF EXISTS {part_name}")
                dropped += 1
        if conn is not None:
            conn.commit()
        return dropped
    finally:
        if owns_cur:
            cur.close()


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

        _log("ticker_types / ticker_related / legacy job_* → Golden Source / Plugin")
        cur.execute("DROP TABLE IF EXISTS ticker_instrument_types CASCADE")
        # ticker_types retired → market.ticker_type (Golden Source / Plugin HTTP)
        _log("ticker_types dropped (Golden Source market.ticker_type)")
        cur.execute("DROP TABLE IF EXISTS public.ticker_types CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.ticker_instrument_types CASCADE")
        # ticker_related_tickers retired → market.ticker_related (Golden Source / FDW)
        _log("ticker_related_tickers dropped (Golden Source market.ticker_related)")
        cur.execute("DROP TABLE IF EXISTS public.ticker_related_tickers CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.stock_related_tickers CASCADE")
        # Retired Trade Celery / legacy job queues — Plugin ingest + analytics.* own these paths.
        _log("job_bars_backfill dropped (Market Data Plugin minute-bars enqueue)")
        cur.execute("DROP TABLE IF EXISTS public.job_bars_backfill CASCADE")
        _log("job_sepa_phase4 dropped (analytics.sepa_screener_wide)")
        cur.execute("DROP TABLE IF EXISTS public.job_sepa_phase4 CASCADE")
        _log("job_ticker_reference_state dropped (Market Data Plugin ticker_sync)")
        cur.execute("DROP TABLE IF EXISTS public.job_ticker_reference_state CASCADE")
        _log("stock_readiness_daily dropped (analytics.sepa_* marts)")
        cur.execute("DROP TABLE IF EXISTS public.stock_readiness_daily CASCADE")
        _log("research_sepa_fundamentals_cache dropped (analytics marts)")
        cur.execute("DROP VIEW IF EXISTS public.v_sepa_symbol_fund_cache_readiness CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.research_sepa_fundamentals_cache CASCADE")
        # P8: source-void ack lives in Golden Source ops_jobs.data_source_void (Market Data Plugin).
        _log("preference_data_gap_ack dropped (ops_jobs.data_source_void)")
        cur.execute("DROP TABLE IF EXISTS public.preference_sepa_gap_ack CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.preference_data_gap_ack CASCADE")
        # One-time: drop legacy stocks / job_stock_reference_state (no Trade ticker tables).
        cur.execute(
            """
            DO $$
            BEGIN
              IF to_regclass('public.stocks') IS NULL THEN
                RETURN;
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
        # preference_data_gap_ack retired → ops_jobs.data_source_void (Market Data Plugin)
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
        _log("strategy_template_param / characteristic / structure_meta → parent jsonb")
        _migrate_strategy_kv_to_jsonb(cur)
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
        _log("strategy_history retired (Wave 3)")
        _retire_strategy_history(cur)
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
        # research_sepa_fundamentals_cache + v_sepa_symbol_fund_cache_readiness retired
        # (analytics marts); DROP on refresh above — no CREATE here.

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

        _log_table(
            "ops_audit_log",
            "Ops control plane audit trail (Wave 4: partitioned timestamptz, 90d retention)",
        )
        _upgrade_ops_audit_log_to_partitioned(cur)
        drop_ops_audit_log_partitions_older_than(cur, cutoff_months=3)

        conn.commit()
