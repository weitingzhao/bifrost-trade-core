"""Naming R3 reverse: undo ``rename_trade_entity`` in one env database (pack §5.4).

Run **before** going back to core < 0.45.0: that core's ``_ensure_tables`` would otherwise run
``ALTER TABLE … ADD CONSTRAINT`` / ``CREATE INDEX`` against the compatibility views and the
db-init Job would fail; and core 0.45.0's own db-init refuses the reversed database (by design),
so the images must go back in the same sitting.

``reverse_sql(env)`` is one transaction for ``bifrost_<env>``, ending ``ROLLBACK`` unless
``commit``: guards (the right database and view owner, ``trade`` a table, ``strategy_instance``
not one) and the counts; drop the six R3 env views and the two compatibility views; every rename
of ``RENAMES`` backwards; the env views exactly as core 0.44.0 built them (embedded below:
0.44.0 cannot be imported once this core is installed); DEV's owner switch and grant; the report,
which stops the transaction when a count differs.

``_V044_ENV_VIEWS`` is ``_create_brokerage_views(cur, "brokerage", env=True)`` of core 0.44.0
(tag v0.44.0, 935e09d), CREATE statements only, generated 2026-10-04. Do not edit it by hand.
"""

from __future__ import annotations

from typing import List, Tuple

from bifrost_core.persistence.postgres.rename_trade_entity import (
    APP_ROLE,
    NEW_ENV_VIEWS,
    OLD_ENV_VIEWS,
    RENAMES,
    as_view_owner,
    before_statement,
    env_guards,
    env_target,
    rename_statement,
    render,
    report_statements,
)

# (label, count before -- new names, count after -- old names).
REVERSE_REPORT: Tuple[Tuple[str, str, str], ...] = (
    ("trade", "SELECT count(*) FROM public.trade", "SELECT count(*) FROM public.strategy_instance"),
    (
        "trade_execution",
        "SELECT count(*) FROM public.trade_execution",
        "SELECT count(*) FROM public.strategy_instance_execution",
    ),
    (
        "splits",
        "SELECT count(*) FROM public.trade_execution WHERE split_quantity IS NOT NULL",
        "SELECT count(*) FROM public.strategy_instance_execution WHERE allocated_quantity IS NOT NULL",
    ),
    (
        "view_attributed",
        "SELECT count(*) FROM brokerage.executions WHERE trade_id IS NOT NULL",
        "SELECT count(*) FROM brokerage.executions WHERE strategy_instance_id IS NOT NULL",
    ),
    (
        "view_splits",
        "SELECT count(*) FROM brokerage.trade_fill_splits",
        "SELECT count(*) FROM brokerage.instance_allocations",
    ),
)

_V044_ENV_VIEWS: tuple[str, ...] = (
    """
CREATE OR REPLACE VIEW brokerage.executions AS SELECT u.account_executions_id, u.account_id, u.exec_id, u.exec_time, u.symbol, u.sec_type, u.side, u.quantity, u.price, u.source, u.expiry, u.strike, u.option_right, u.exchange, u.order_id, u.cum_qty, u.contract_key, u.currency, u.asset_category, u.sub_category, u.description, u.conid, u.security_id, u.security_id_type, u.cusip, u.isin, u.figi, u.listing_exchange, u.underlying_conid, u.underlying_symbol, u.underlying_security_id, u.underlying_listing_exchange, u.issuer, u.issuer_country_code, u.trade_id, u.related_trade_id, u.report_date, u.trade_date, u.settle_date_target, u.transaction_type, u.multiplier, u.principal_adjust_factor, u.proceeds, u.taxes, u.net_cash, u.close_price, u.open_close_indicator, u.notes, u.cost, u.fifo_pnl_realized, u.mtm_pnl, u.trade_money, u.fx_rate_to_base, u.acct_alias, u.model, u.raw_extra, si.strategy_opportunity_id, sie.strategy_instance_id, u.created_at
        FROM (
        SELECT executions_raw_flex_id AS account_executions_id,
               account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, currency, asset_category, sub_category, description, conid, security_id, security_id_type, cusip, isin, figi, listing_exchange, underlying_conid, underlying_symbol, underlying_security_id, underlying_listing_exchange, issuer, issuer_country_code, trade_id, related_trade_id, report_date, trade_date, settle_date_target, transaction_type, multiplier, principal_adjust_factor, proceeds, taxes, net_cash, close_price, open_close_indicator, notes, cost, fifo_pnl_realized, mtm_pnl, trade_money, fx_rate_to_base, acct_alias, model, raw_extra, strategy_opportunity_id, strategy_instance_id, created_at
        FROM brokerage.executions_raw_flex
        UNION ALL
        SELECT -(executions_raw_tws_id) AS account_executions_id,
               account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, currency, asset_category, sub_category, description, conid, security_id, security_id_type, cusip, isin, figi, listing_exchange, underlying_conid, underlying_symbol, underlying_security_id, underlying_listing_exchange, issuer, issuer_country_code, trade_id, related_trade_id, report_date, trade_date, settle_date_target, transaction_type, multiplier, principal_adjust_factor, proceeds, taxes, net_cash, close_price, open_close_indicator, notes, cost, fifo_pnl_realized, mtm_pnl, trade_money, fx_rate_to_base, acct_alias, model, raw_extra, strategy_opportunity_id, strategy_instance_id, created_at
        FROM brokerage.executions_raw_tws t
        WHERE NOT EXISTS (
            SELECT 1 FROM brokerage.executions_raw_flex f
            WHERE f.exec_id = t.exec_id
              AND f.exec_id IS NOT NULL AND f.exec_id != ''
              AND t.exec_id IS NOT NULL AND t.exec_id != ''
        )
        UNION ALL
        SELECT -(1000000000 + executions_raw_journal_id) AS account_executions_id,
               account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, currency, asset_category, sub_category, description, conid, security_id, security_id_type, cusip, isin, figi, listing_exchange, underlying_conid, underlying_symbol, underlying_security_id, underlying_listing_exchange, issuer, issuer_country_code, trade_id, related_trade_id, report_date, trade_date, settle_date_target, transaction_type, multiplier, principal_adjust_factor, proceeds, taxes, net_cash, close_price, open_close_indicator, notes, cost, fifo_pnl_realized, mtm_pnl, trade_money, fx_rate_to_base, acct_alias, model, raw_extra, strategy_opportunity_id, strategy_instance_id, created_at
        FROM brokerage.executions_raw_journal
        ) u
        LEFT JOIN public.strategy_instance_execution sie
          ON sie.account_id = u.account_id AND sie.exec_id = u.exec_id
         AND sie.allocated_quantity IS NULL
        LEFT JOIN public.strategy_instance si ON si.strategy_instance_id = sie.strategy_instance_id
""",
    """
CREATE OR REPLACE VIEW brokerage.executions_final AS SELECT u.account_executions_id, u.account_id, u.exec_id, u.exec_time, u.symbol, u.sec_type, u.side, u.quantity, u.price, u.source, u.expiry, u.strike, u.option_right, u.exchange, u.order_id, u.cum_qty, u.contract_key, u.currency, u.asset_category, u.sub_category, u.description, u.conid, u.security_id, u.security_id_type, u.cusip, u.isin, u.figi, u.listing_exchange, u.underlying_conid, u.underlying_symbol, u.underlying_security_id, u.underlying_listing_exchange, u.issuer, u.issuer_country_code, u.trade_id, u.related_trade_id, u.report_date, u.trade_date, u.settle_date_target, u.transaction_type, u.multiplier, u.principal_adjust_factor, u.proceeds, u.taxes, u.net_cash, u.close_price, u.open_close_indicator, u.notes, u.cost, u.fifo_pnl_realized, u.mtm_pnl, u.trade_money, u.fx_rate_to_base, u.acct_alias, u.model, u.raw_extra, si.strategy_opportunity_id, sie.strategy_instance_id, u.created_at
        FROM (
        SELECT executions_raw_flex_id AS account_executions_id,
               account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, currency, asset_category, sub_category, description, conid, security_id, security_id_type, cusip, isin, figi, listing_exchange, underlying_conid, underlying_symbol, underlying_security_id, underlying_listing_exchange, issuer, issuer_country_code, trade_id, related_trade_id, report_date, trade_date, settle_date_target, transaction_type, multiplier, principal_adjust_factor, proceeds, taxes, net_cash, close_price, open_close_indicator, notes, cost, fifo_pnl_realized, mtm_pnl, trade_money, fx_rate_to_base, acct_alias, model, raw_extra, strategy_opportunity_id, strategy_instance_id, created_at
        FROM brokerage.executions_raw_flex
        UNION ALL
        SELECT -(1000000000 + executions_raw_journal_id) AS account_executions_id,
               account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, currency, asset_category, sub_category, description, conid, security_id, security_id_type, cusip, isin, figi, listing_exchange, underlying_conid, underlying_symbol, underlying_security_id, underlying_listing_exchange, issuer, issuer_country_code, trade_id, related_trade_id, report_date, trade_date, settle_date_target, transaction_type, multiplier, principal_adjust_factor, proceeds, taxes, net_cash, close_price, open_close_indicator, notes, cost, fifo_pnl_realized, mtm_pnl, trade_money, fx_rate_to_base, acct_alias, model, raw_extra, strategy_opportunity_id, strategy_instance_id, created_at
        FROM brokerage.executions_raw_journal
        ) u
        LEFT JOIN public.strategy_instance_execution sie
          ON sie.account_id = u.account_id AND sie.exec_id = u.exec_id
         AND sie.allocated_quantity IS NULL
        LEFT JOIN public.strategy_instance si ON si.strategy_instance_id = sie.strategy_instance_id
""",
    """
CREATE OR REPLACE VIEW brokerage.executions_fly AS SELECT u.account_executions_id, u.account_id, u.exec_id, u.exec_time, u.symbol, u.sec_type, u.side, u.quantity, u.price, u.source, u.expiry, u.strike, u.option_right, u.exchange, u.order_id, u.cum_qty, u.contract_key, u.currency, u.asset_category, u.sub_category, u.description, u.conid, u.security_id, u.security_id_type, u.cusip, u.isin, u.figi, u.listing_exchange, u.underlying_conid, u.underlying_symbol, u.underlying_security_id, u.underlying_listing_exchange, u.issuer, u.issuer_country_code, u.trade_id, u.related_trade_id, u.report_date, u.trade_date, u.settle_date_target, u.transaction_type, u.multiplier, u.principal_adjust_factor, u.proceeds, u.taxes, u.net_cash, u.close_price, u.open_close_indicator, u.notes, u.cost, u.fifo_pnl_realized, u.mtm_pnl, u.trade_money, u.fx_rate_to_base, u.acct_alias, u.model, u.raw_extra, si.strategy_opportunity_id, sie.strategy_instance_id, u.created_at
        FROM (
        SELECT -(t.executions_raw_tws_id) AS account_executions_id,
               t.account_id, t.exec_id, t.exec_time, t.symbol, t.sec_type, t.side, t.quantity, t.price, t.source, t.expiry, t.strike, t.option_right, t.exchange, t.order_id, t.cum_qty, t.contract_key, t.currency, t.asset_category, t.sub_category, t.description, t.conid, t.security_id, t.security_id_type, t.cusip, t.isin, t.figi, t.listing_exchange, t.underlying_conid, t.underlying_symbol, t.underlying_security_id, t.underlying_listing_exchange, t.issuer, t.issuer_country_code, t.trade_id, t.related_trade_id, t.report_date, t.trade_date, t.settle_date_target, t.transaction_type, t.multiplier, t.principal_adjust_factor, t.proceeds, t.taxes, t.net_cash, t.close_price, t.open_close_indicator, t.notes, t.cost, t.fifo_pnl_realized, t.mtm_pnl, t.trade_money, t.fx_rate_to_base, t.acct_alias, t.model, t.raw_extra, t.strategy_opportunity_id, t.strategy_instance_id, t.created_at
        FROM brokerage.executions_raw_tws t
        WHERE upper(trim(COALESCE(t.sec_type, ''))) <> 'BAG'
          AND NOT EXISTS (
            SELECT 1
            FROM brokerage.executions_final f
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
                  AND upper(trim(COALESCE(NULLIF(trim(COALESCE(f.sec_type, '')), ''), NULLIF(trim(split_part(COALESCE(f.contract_key, ''), '|', 2)), '')))) IN ('STK', 'EQUITY', 'FUND', 'ETF', 'ETN', 'ADR', 'CORP', 'STOCK', 'REIT', 'WAR')
                  AND NULLIF(trim(COALESCE(t.symbol, '')), '') IS NOT NULL
                  AND upper(trim(COALESCE(t.symbol, ''))) = upper(trim(COALESCE(f.symbol, '')))
                )
              )
        )
        ) u
        LEFT JOIN public.strategy_instance_execution sie
          ON sie.account_id = u.account_id AND sie.exec_id = u.exec_id
         AND sie.allocated_quantity IS NULL
        LEFT JOIN public.strategy_instance si ON si.strategy_instance_id = sie.strategy_instance_id
""",
    """
CREATE OR REPLACE VIEW brokerage.executions_tws AS SELECT u.account_executions_id, u.account_id, u.exec_id, u.exec_time, u.symbol, u.sec_type, u.side, u.quantity, u.price, u.source, u.expiry, u.strike, u.option_right, u.exchange, u.order_id, u.cum_qty, u.contract_key, u.currency, u.asset_category, u.sub_category, u.description, u.conid, u.security_id, u.security_id_type, u.cusip, u.isin, u.figi, u.listing_exchange, u.underlying_conid, u.underlying_symbol, u.underlying_security_id, u.underlying_listing_exchange, u.issuer, u.issuer_country_code, u.trade_id, u.related_trade_id, u.report_date, u.trade_date, u.settle_date_target, u.transaction_type, u.multiplier, u.principal_adjust_factor, u.proceeds, u.taxes, u.net_cash, u.close_price, u.open_close_indicator, u.notes, u.cost, u.fifo_pnl_realized, u.mtm_pnl, u.trade_money, u.fx_rate_to_base, u.acct_alias, u.model, u.raw_extra, si.strategy_opportunity_id, sie.strategy_instance_id, u.created_at
        FROM (
        SELECT -(executions_raw_tws_id) AS account_executions_id,
               account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, currency, asset_category, sub_category, description, conid, security_id, security_id_type, cusip, isin, figi, listing_exchange, underlying_conid, underlying_symbol, underlying_security_id, underlying_listing_exchange, issuer, issuer_country_code, trade_id, related_trade_id, report_date, trade_date, settle_date_target, transaction_type, multiplier, principal_adjust_factor, proceeds, taxes, net_cash, close_price, open_close_indicator, notes, cost, fifo_pnl_realized, mtm_pnl, trade_money, fx_rate_to_base, acct_alias, model, raw_extra, strategy_opportunity_id, strategy_instance_id, created_at
        FROM brokerage.executions_raw_tws
        ) u
        LEFT JOIN public.strategy_instance_execution sie
          ON sie.account_id = u.account_id AND sie.exec_id = u.exec_id
         AND sie.allocated_quantity IS NULL
        LEFT JOIN public.strategy_instance si ON si.strategy_instance_id = sie.strategy_instance_id
""",
    """
CREATE OR REPLACE VIEW brokerage.instance_allocations AS
SELECT s.account_id, x.account_executions_id, s.strategy_instance_id,
       s.allocated_quantity::double precision AS allocated_quantity,
       s.exec_id
FROM public.strategy_instance_execution s
JOIN (
    SELECT executions_raw_flex_id AS account_executions_id, account_id, exec_id
    FROM brokerage.executions_raw_flex
    UNION ALL
    SELECT -(executions_raw_tws_id), account_id, exec_id
    FROM brokerage.executions_raw_tws
    UNION ALL
    SELECT -(1000000000 + executions_raw_journal_id), account_id, exec_id
    FROM brokerage.executions_raw_journal
) x ON x.account_id = s.account_id AND x.exec_id = s.exec_id
WHERE s.allocated_quantity IS NOT NULL
""",
)


def reverse_statements(env: str) -> List[str]:
    """Every statement of the reverse transaction, without BEGIN / COMMIT."""
    parts: List[str] = ["SET LOCAL lock_timeout = '5s'", f"SET LOCAL ROLE {APP_ROLE}"]
    parts += env_guards(env)
    parts.append(
        """DO $r3$ BEGIN
  IF (SELECT relkind FROM pg_class WHERE oid = to_regclass('public.trade')) IS DISTINCT FROM 'r' THEN
    RAISE EXCEPTION 'R3 reverse: public.trade is not a table (not migrated, or already reversed?)';
  END IF;
  IF (SELECT relkind FROM pg_class WHERE oid = to_regclass('public.strategy_instance')) = 'r' THEN
    RAISE EXCEPTION 'R3 reverse: public.strategy_instance is a table already';
  END IF;
END $r3$"""
    )
    parts += ["DROP TABLE IF EXISTS pg_temp.r3_before", before_statement(REVERSE_REPORT)]
    parts += as_view_owner(env, [f"DROP VIEW {v}" for v in NEW_ENV_VIEWS], (), grant=False)
    parts += [
        "DROP VIEW IF EXISTS public.strategy_instance_execution",
        "DROP VIEW IF EXISTS public.strategy_instance",
    ]
    # Backwards: each statement still names its table by the R3 name (trade, trade_execution),
    # because the tables themselves are renamed back last.
    parts += [rename_statement(kind, table, new, old) for kind, table, old, new in reversed(RENAMES)]
    parts += as_view_owner(env, [v.strip() for v in _V044_ENV_VIEWS], OLD_ENV_VIEWS, grant=True)
    parts += report_statements(REVERSE_REPORT)
    return parts


def reverse_sql(env: str, *, commit: bool = False) -> str:
    """The R3 reverse for ``bifrost_<env>``, one transaction; ROLLBACK unless ``commit``."""
    db, _ = env_target(env)
    return render(reverse_statements(env), commit=commit, title=f"naming R3 REVERSE, {db}, back to core 0.44.0 names")


__all__ = ["REVERSE_REPORT", "reverse_sql", "reverse_statements"]
