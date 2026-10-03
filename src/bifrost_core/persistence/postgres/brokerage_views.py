"""The three execution views over the raw execution tables, and the per-env variants.

Split out of ``brokerage_ddl`` (which keeps re-exporting these names) so that module
stays under the 800-line code-health limit; nothing here changed when it moved.
Golden Source builds the views over ``raw_broker.executions_raw_*``; each env builds
them over its FDW tables with ``env=True`` (attribution from strategy_instance_execution,
TD-09).
"""

from __future__ import annotations

from typing import Any

from bifrost_core.persistence.postgres.brokerage_tables import (
    BROKERAGE_ENV_VIEWS,
    INSTANCE_EXECUTION,
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


def _env_attributed(rows_sql: str) -> str:
    """Wrap a set of raw execution rows with this env's attribution (TD-09).

    ``rows_sql`` selects ``account_executions_id`` plus the canonical columns. The two
    Golden Source attribution columns are replaced: ``strategy_instance_id`` from the
    whole-fill row of ``public.strategy_instance_execution`` on (account_id, exec_id),
    ``strategy_opportunity_id`` from that instance. Column names and order are unchanged.
    """
    cols = [c.strip() for c in _EXEC_CANONICAL_COLS.split(",") if c.strip()]
    out = []
    for c in cols:
        if c == "strategy_instance_id":
            out.append("sie.strategy_instance_id")
        elif c == "strategy_opportunity_id":
            out.append("si.strategy_opportunity_id")
        else:
            out.append(f"u.{c}")
    return (
        f"SELECT u.account_executions_id, {', '.join(out)}\n"
        f"        FROM ({rows_sql}) u\n"
        f"        LEFT JOIN public.{INSTANCE_EXECUTION} sie\n"
        "          ON sie.account_id = u.account_id AND sie.exec_id = u.exec_id\n"
        "         AND sie.allocated_quantity IS NULL\n"
        "        LEFT JOIN public.strategy_instance si ON si.strategy_instance_id = sie.strategy_instance_id"
    )


def _create_brokerage_views(cur: Any, schema: str, *, env: bool = False) -> None:
    """The execution views over the three raw tables.

    Golden Source (``env=False``): the attribution columns are the raw tables' own (no
    longer written since TD-09; kept for the rollback window). Per-env DBs (``env=True``,
    over the FDW tables): attribution comes from this env's ``strategy_instance_execution``,
    and two env-only views are added -- ``executions_tws`` and ``instance_allocations``.
    """
    cols = _EXEC_CANONICAL_COLS
    for name in BROKERAGE_ENV_VIEWS:
        cur.execute(f"DROP VIEW IF EXISTS {schema}.{name} CASCADE")
    cur.execute(f"DROP VIEW IF EXISTS {schema}.executions_fly CASCADE")
    cur.execute(f"DROP VIEW IF EXISTS {schema}.executions_final CASCADE")
    cur.execute(f"DROP VIEW IF EXISTS {schema}.executions CASCADE")

    def body(rows_sql: str) -> str:
        return _env_attributed(rows_sql) if env else rows_sql

    executions_rows = f"""
        SELECT executions_raw_flex_id AS account_executions_id,
               {cols}
        FROM {schema}.executions_raw_flex
        UNION ALL
        SELECT -(executions_raw_tws_id) AS account_executions_id,
               {cols}
        FROM {schema}.executions_raw_tws t
        WHERE NOT EXISTS (
            SELECT 1 FROM {schema}.executions_raw_flex f
            WHERE f.exec_id = t.exec_id
              AND f.exec_id IS NOT NULL AND f.exec_id != ''
              AND t.exec_id IS NOT NULL AND t.exec_id != ''
        )
        UNION ALL
        SELECT -(1000000000 + executions_raw_journal_id) AS account_executions_id,
               {cols}
        FROM {schema}.executions_raw_journal
        """
    cur.execute(f"CREATE OR REPLACE VIEW {schema}.executions AS {body(executions_rows)}")

    final_rows = f"""
        SELECT executions_raw_flex_id AS account_executions_id,
               {cols}
        FROM {schema}.executions_raw_flex
        UNION ALL
        SELECT -(1000000000 + executions_raw_journal_id) AS account_executions_id,
               {cols}
        FROM {schema}.executions_raw_journal
        """
    cur.execute(f"CREATE OR REPLACE VIEW {schema}.executions_final AS {body(final_rows)}")

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
    fly_rows = f"""
        SELECT -(t.executions_raw_tws_id) AS account_executions_id,
               {exec_cols_t}
        FROM {schema}.executions_raw_tws t
        WHERE upper(trim(COALESCE(t.sec_type, ''))) <> 'BAG'
          AND NOT EXISTS (
            SELECT 1
            FROM {schema}.executions_final f
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
    cur.execute(f"CREATE OR REPLACE VIEW {schema}.executions_fly AS {body(fly_rows)}")

    if not env:
        return

    tws_rows = f"""
        SELECT -(executions_raw_tws_id) AS account_executions_id,
               {cols}
        FROM {schema}.executions_raw_tws
        """
    cur.execute(f"CREATE OR REPLACE VIEW {schema}.executions_tws AS {body(tws_rows)}")

    # Split rows, one per raw representation of the fill, in the shape readers joined
    # account_execution_instance_allocation by (account_executions_id, account_id).
    cur.execute(
        f"""
        CREATE OR REPLACE VIEW {schema}.instance_allocations AS
        SELECT s.account_id, x.account_executions_id, s.strategy_instance_id,
               s.allocated_quantity::double precision AS allocated_quantity,
               s.exec_id
        FROM public.{INSTANCE_EXECUTION} s
        JOIN (
            SELECT executions_raw_flex_id AS account_executions_id, account_id, exec_id
            FROM {schema}.executions_raw_flex
            UNION ALL
            SELECT -(executions_raw_tws_id), account_id, exec_id
            FROM {schema}.executions_raw_tws
            UNION ALL
            SELECT -(1000000000 + executions_raw_journal_id), account_id, exec_id
            FROM {schema}.executions_raw_journal
        ) x ON x.account_id = s.account_id AND x.exec_id = s.exec_id
        WHERE s.allocated_quantity IS NOT NULL
        """
    )
