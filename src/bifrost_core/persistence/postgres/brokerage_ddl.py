"""DDL for bifrost_golden_source.brokerage.* + per-env postgres_fdw setup.

Brokerage Golden Source holds IB/brokerage account data (accounts, positions,
executions, commissions, cash transactions, open orders, live quotes, Flex settings).
Per-env databases (bifrost_dev / bifrost_prod) import these as foreign tables so
readers can JOIN with public strategy/preference tables on a single connection.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from bifrost_core.persistence.postgres.brokerage_tables import (
    BROKERAGE_PHYSICAL_TABLES,
    BROKERAGE_VIEWS,
    SCHEMA,
)
from bifrost_core.persistence.postgres.market_tables import (
    MARKET_FOREIGN_TABLES,
    MARKET_LOCAL_VIEWS,
    SCHEMA as MARKET_SCHEMA,
)

logger = logging.getLogger(__name__)

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

_EXEC_RAW_COLUMNS_DDL = """
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
"""


def ensure_brokerage_schema(
    conn: Any,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Create brokerage schema + tables + views on bifrost_golden_source."""
    _log = log or (lambda m: logger.info("%s", m))
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        _log(f"schema {SCHEMA}")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.account (
                account_id text PRIMARY KEY,
                updated_at timestamptz DEFAULT now(),
                net_liquidation double precision,
                total_cash double precision,
                buying_power double precision,
                summary_extra jsonb
            )
            """
        )
        _log(f"{SCHEMA}.account")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.positions (
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
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS brokerage_positions_account_contract_key
            ON {SCHEMA}.positions (account_id, contract_key)
            """
        )
        _log(f"{SCHEMA}.positions")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.contract_quote_live (
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
        _log(f"{SCHEMA}.contract_quote_live")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.commissions (
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
        _log(f"{SCHEMA}.commissions")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.transactions (
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
            f"CREATE INDEX IF NOT EXISTS brokerage_transactions_account_ts "
            f"ON {SCHEMA}.transactions (account_id, ts DESC)"
        )
        _log(f"{SCHEMA}.transactions")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.open_orders (
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
        _log(f"{SCHEMA}.open_orders")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.settings_flex (
                id serial PRIMARY KEY,
                sort_order integer NOT NULL DEFAULT 0,
                query_label text,
                purpose text DEFAULT 'cash_transactions',
                query_host_id text NOT NULL,
                query_secondary_id text
            )
            """
        )
        _log(f"{SCHEMA}.settings_flex")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.executions_raw_tws (
                executions_raw_tws_id bigserial PRIMARY KEY,
                {_EXEC_RAW_COLUMNS_DDL}
            )
            """
        )
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS brokerage_executions_raw_tws_exec_id_key "
            f"ON {SCHEMA}.executions_raw_tws (exec_id) "
            f"WHERE exec_id IS NOT NULL AND exec_id != ''"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS brokerage_executions_raw_tws_account_time "
            f"ON {SCHEMA}.executions_raw_tws (account_id, exec_time DESC)"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS brokerage_executions_raw_tws_contract_key "
            f"ON {SCHEMA}.executions_raw_tws (account_id, contract_key) "
            f"WHERE contract_key IS NOT NULL"
        )
        _log(f"{SCHEMA}.executions_raw_tws")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.executions_raw_flex (
                executions_raw_flex_id bigserial PRIMARY KEY,
                {_EXEC_RAW_COLUMNS_DDL.replace("source text,", "source text DEFAULT 'flex_trades',", 1)}
            )
            """
        )
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS brokerage_executions_raw_flex_exec_id_key "
            f"ON {SCHEMA}.executions_raw_flex (exec_id) "
            f"WHERE exec_id IS NOT NULL AND exec_id != ''"
        )
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS brokerage_executions_raw_flex_account_trade_id_key "
            f"ON {SCHEMA}.executions_raw_flex (account_id, trade_id) "
            f"WHERE trade_id IS NOT NULL AND trade_id != ''"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS brokerage_executions_raw_flex_account_time "
            f"ON {SCHEMA}.executions_raw_flex (account_id, exec_time DESC)"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS brokerage_executions_raw_flex_contract_key "
            f"ON {SCHEMA}.executions_raw_flex (account_id, contract_key) "
            f"WHERE contract_key IS NOT NULL"
        )
        _log(f"{SCHEMA}.executions_raw_flex")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCHEMA}.executions_raw_journal (
                executions_raw_journal_id bigserial PRIMARY KEY,
                {_EXEC_RAW_COLUMNS_DDL}
            )
            """
        )
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS brokerage_executions_raw_journal_exec_id_key "
            f"ON {SCHEMA}.executions_raw_journal (exec_id) "
            f"WHERE exec_id IS NOT NULL AND exec_id != ''"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS brokerage_executions_raw_journal_account_time "
            f"ON {SCHEMA}.executions_raw_journal (account_id, exec_time DESC)"
        )
        _log(f"{SCHEMA}.executions_raw_journal")

        _create_brokerage_views(cur)
        _log(f"{SCHEMA}.executions / executions_final / executions_fly views")

        _grant_brokerage_privileges(cur)
        _log("granted privileges (best-effort)")

    conn.commit()


def _create_brokerage_views(cur: Any) -> None:
    cols = _EXEC_CANONICAL_COLS
    cur.execute(f"DROP VIEW IF EXISTS {SCHEMA}.executions_fly CASCADE")
    cur.execute(f"DROP VIEW IF EXISTS {SCHEMA}.executions_final CASCADE")
    cur.execute(f"DROP VIEW IF EXISTS {SCHEMA}.executions CASCADE")

    cur.execute(
        f"""
        CREATE OR REPLACE VIEW {SCHEMA}.executions AS
        SELECT executions_raw_flex_id AS account_executions_id,
               {cols}
        FROM {SCHEMA}.executions_raw_flex
        UNION ALL
        SELECT -(executions_raw_tws_id) AS account_executions_id,
               {cols}
        FROM {SCHEMA}.executions_raw_tws t
        WHERE NOT EXISTS (
            SELECT 1 FROM {SCHEMA}.executions_raw_flex f
            WHERE f.exec_id = t.exec_id
              AND f.exec_id IS NOT NULL AND f.exec_id != ''
              AND t.exec_id IS NOT NULL AND t.exec_id != ''
        )
        UNION ALL
        SELECT -(1000000000 + executions_raw_journal_id) AS account_executions_id,
               {cols}
        FROM {SCHEMA}.executions_raw_journal
        """
    )

    cur.execute(
        f"""
        CREATE OR REPLACE VIEW {SCHEMA}.executions_final AS
        SELECT executions_raw_flex_id AS account_executions_id,
               {cols}
        FROM {SCHEMA}.executions_raw_flex
        UNION ALL
        SELECT -(1000000000 + executions_raw_journal_id) AS account_executions_id,
               {cols}
        FROM {SCHEMA}.executions_raw_journal
        """
    )

    exec_cols_t = ", ".join(f"t.{c.strip()}" for c in cols.split(",") if c.strip())
    fly_final_equity = (
        "'STK', 'EQUITY', 'FUND', 'ETF', 'ETN', 'ADR', 'CORP', 'STOCK', 'REIT', 'WAR'"
    )
    fly_f_sec_norm = (
        "upper(trim(COALESCE("
        "NULLIF(trim(COALESCE(f.sec_type, '')), ''), "
        "NULLIF(trim(split_part(COALESCE(f.contract_key, ''), '|', 2)), '')"
        ")))"
    )
    cur.execute(
        f"""
        CREATE OR REPLACE VIEW {SCHEMA}.executions_fly AS
        SELECT -(t.executions_raw_tws_id) AS account_executions_id,
               {exec_cols_t}
        FROM {SCHEMA}.executions_raw_tws t
        WHERE upper(trim(COALESCE(t.sec_type, ''))) <> 'BAG'
          AND NOT EXISTS (
            SELECT 1
            FROM {SCHEMA}.executions_final f
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
                  AND rtrim(trim(COALESCE(t.contract_key, '')), '|')
                      = rtrim(trim(COALESCE(f.contract_key, '')), '|')
                )
                OR (
                  upper(trim(COALESCE(t.sec_type, ''))) = 'STK'
                  AND {fly_f_sec_norm} IN ({fly_final_equity})
                  AND NULLIF(trim(COALESCE(t.symbol, '')), '') IS NOT NULL
                  AND upper(trim(COALESCE(t.symbol, ''))) = upper(trim(COALESCE(f.symbol, '')))
                )
              )
        )
        """
    )


def _grant_brokerage_privileges(cur: Any) -> None:
    """Grant to known roles when present (roles may need superuser to create)."""
    for role in ("brokerage_writer", "brokerage_reader", "bifrost", "data_writer"):
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if not cur.fetchone():
            continue
        cur.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {role}")
        if role in ("brokerage_writer", "bifrost", "data_writer"):
            cur.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES "
                f"IN SCHEMA {SCHEMA} TO {role}"
            )
            cur.execute(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {SCHEMA} TO {role}"
            )
            cur.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
            )
            cur.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
                f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
            )
        else:
            cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {role}")
            cur.execute(
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
                f"GRANT SELECT ON TABLES TO {role}"
            )


def apply_brokerage_roles_sql() -> str:
    """SQL for elevated (superuser) role bootstrap. Run once on CNPG primary."""
    return """
-- Brokerage Golden Source roles (run as postgres on bifrost_golden_source)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brokerage_writer') THEN
    CREATE ROLE brokerage_writer LOGIN PASSWORD 'CHANGE_ME';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brokerage_reader') THEN
    CREATE ROLE brokerage_reader LOGIN PASSWORD 'CHANGE_ME';
  END IF;
END $$;
GRANT CONNECT ON DATABASE bifrost_golden_source TO brokerage_writer, brokerage_reader;
GRANT CONNECT ON DATABASE bifrost_dev TO brokerage_reader;
GRANT CONNECT ON DATABASE bifrost_prod TO brokerage_reader;
"""


def setup_fdw_foreign_tables(
    env_conn: Any,
    golden_source_params: dict[str, Any],
    *,
    local_user: str = "bifrost",
    server_name: str = "golden_source_server",
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Import brokerage.* from golden_source into the current per-env database via FDW.

    Requires superuser (or equivalent) for CREATE EXTENSION / CREATE SERVER.
    ``golden_source_params`` uses psycopg2 connect keys: host, port, dbname, user, password.
    """
    _log = log or (lambda m: logger.info("%s", m))
    host = str(golden_source_params.get("host") or "127.0.0.1")
    port = str(int(golden_source_params.get("port") or 5432))
    dbname = str(golden_source_params.get("dbname") or "bifrost_golden_source")
    remote_user = str(golden_source_params.get("user") or "brokerage_reader")
    remote_password = str(golden_source_params.get("password") or "")

    with env_conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
        _log("extension postgres_fdw")

        cur.execute(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_foreign_server WHERE srvname = '{server_name}'
              ) THEN
                CREATE SERVER {server_name}
                  FOREIGN DATA WRAPPER postgres_fdw
                  OPTIONS (host '{host}', port '{port}', dbname '{dbname}');
              ELSE
                ALTER SERVER {server_name}
                  OPTIONS (
                    SET host '{host}',
                    SET port '{port}',
                    SET dbname '{dbname}'
                  );
              END IF;
            END $$;
            """
        )
        _log(f"server {server_name}")

        pw_sql = remote_password.replace("'", "''")
        cur.execute(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_user_mappings um
                JOIN pg_foreign_server s ON s.oid = um.srvid
                JOIN pg_roles r ON r.oid = um.umuser
                WHERE s.srvname = '{server_name}' AND r.rolname = '{local_user}'
              ) THEN
                CREATE USER MAPPING FOR {local_user}
                  SERVER {server_name}
                  OPTIONS (user '{remote_user}', password '{pw_sql}');
              ELSE
                ALTER USER MAPPING FOR {local_user}
                  SERVER {server_name}
                  OPTIONS (SET user '{remote_user}', SET password '{pw_sql}');
              END IF;
            END $$;
            """
        )
        _log(f"user mapping for {local_user}")

        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

        for name in list(BROKERAGE_VIEWS) + list(BROKERAGE_PHYSICAL_TABLES):
            cur.execute(f"DROP FOREIGN TABLE IF EXISTS {SCHEMA}.{name} CASCADE")
            cur.execute(f"DROP VIEW IF EXISTS {SCHEMA}.{name} CASCADE")
            cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.{name} CASCADE")

        table_list = ", ".join(BROKERAGE_PHYSICAL_TABLES)
        cur.execute(
            f"""
            IMPORT FOREIGN SCHEMA {SCHEMA}
              LIMIT TO ({table_list})
              FROM SERVER {server_name}
              INTO {SCHEMA}
            """
        )
        _log(f"imported foreign tables: {table_list}")

        _create_brokerage_views(cur)
        _log("local views over foreign tables")

        cur.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {local_user}")
        cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {local_user}")

    env_conn.commit()


def setup_fdw_market_tables(
    env_conn: Any,
    *,
    server_name: str = "golden_source_server",
    local_user: str = "bifrost",
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Import market.ticker / us_market_holiday and create universe views via FDW.

    Assumes ``golden_source_server`` + user mapping already exist (created by
    ``setup_fdw_foreign_tables``). Call this *after* the brokerage FDW setup.
    """
    _log = log or (lambda m: logger.info("%s", m))

    with env_conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {MARKET_SCHEMA}")

        for name in list(MARKET_LOCAL_VIEWS) + list(MARKET_FOREIGN_TABLES):
            cur.execute(f"DROP FOREIGN TABLE IF EXISTS {MARKET_SCHEMA}.{name} CASCADE")
            cur.execute(f"DROP VIEW IF EXISTS {MARKET_SCHEMA}.{name} CASCADE")
            cur.execute(f"DROP TABLE IF EXISTS {MARKET_SCHEMA}.{name} CASCADE")

        table_list = ", ".join(MARKET_FOREIGN_TABLES)
        cur.execute(
            f"""
            IMPORT FOREIGN SCHEMA {MARKET_SCHEMA}
              LIMIT TO ({table_list})
              FROM SERVER {server_name}
              INTO {MARKET_SCHEMA}
            """
        )
        _log(f"imported market foreign tables: {table_list}")

        # Local view matching Golden Source market.v_us_equity_universe definition
        cur.execute(
            f"""
            CREATE OR REPLACE VIEW {MARKET_SCHEMA}.v_us_equity_universe AS
            SELECT symbol, name, market, locale, primary_exchange,
                   instrument_type, active, sector, industry, list_date, market_cap
            FROM {MARKET_SCHEMA}.ticker
            WHERE COALESCE(active, false) = true
              AND lower(COALESCE(locale, '')) = 'us'
              AND lower(COALESCE(market, '')) = 'stocks'
              AND lower(COALESCE(instrument_type, '')) = 'cs'
            """
        )
        _log(f"{MARKET_SCHEMA}.v_us_equity_universe view")

        # Drop legacy public views/tables before creating backward-compat view
        cur.execute("DROP VIEW IF EXISTS public.v_sepa_us_equity_universe CASCADE")
        cur.execute("DROP VIEW IF EXISTS public.v_us_equity_universe CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.us_equity_universe CASCADE")

        # Backward-compat view: all existing SQL references public.v_us_equity_universe
        cur.execute(
            f"""
            CREATE OR REPLACE VIEW public.v_us_equity_universe AS
            SELECT *,
                   hashtext(upper(trim(symbol)))::bigint AS tickers_id
            FROM {MARKET_SCHEMA}.v_us_equity_universe
            """
        )
        _log("public.v_us_equity_universe backward-compat view")

        # Drop legacy price readiness table/view (no longer needed)
        cur.execute("DROP VIEW IF EXISTS public.v_sepa_symbol_price_readiness CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.sepa_symbol_price_readiness CASCADE")
        _log("dropped legacy price readiness table/view")

        cur.execute(f"GRANT USAGE ON SCHEMA {MARKET_SCHEMA} TO {local_user}")
        cur.execute(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {MARKET_SCHEMA} TO {local_user}"
        )

    env_conn.commit()
