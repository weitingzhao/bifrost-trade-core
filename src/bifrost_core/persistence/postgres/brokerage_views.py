"""The three execution views over the raw execution tables, and the per-env variants.

Split out of ``brokerage_ddl`` (which keeps re-exporting these names) so that module
stays under the 800-line code-health limit; nothing here changed when it moved.
Golden Source builds the views over ``raw_broker.executions_raw_*``; each env builds
them over its FDW tables with ``env=True`` (attribution from trade_execution, TD-09).

Naming R3 (core 0.45.0) changed the env views' columns, not Golden Source's: the
attribution is ``trade_id`` (the Trade), IB's TradeID / RelatedTradeID are ``ib_trade_id``
/ ``ib_related_trade_id``. Golden Source's views keep the vendor names. The one-version
``strategy_instance_id`` (= ``trade_id``) column and ``brokerage.instance_allocations`` went in
naming R4 (core 0.47.0). ``setup_fdw_foreign_tables`` drops and rebuilds these views, but in
dev / stg / prod db-init's FDW step stops before it (``must be owner of foreign server``), so
the views there are rebuilt by the Owner's R4 step (``drop_trade_compat``), as R3's were.
"""

from __future__ import annotations

from typing import Any

from bifrost_core.persistence.postgres.brokerage_tables import (
    BROKERAGE_ENV_VIEWS,
    TRADE_EXECUTION,
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


# Env view output for the raw columns renamed by naming R3 (core 0.45.0). The Golden Source
# attribution column ``strategy_instance_id`` (frozen) becomes this env's ``trade_id``; the
# opportunity is the trade's.
_ENV_RENAMED = {
    "trade_id": "u.trade_id AS ib_trade_id",
    "related_trade_id": "u.related_trade_id AS ib_related_trade_id",
    "strategy_opportunity_id": "tr.strategy_opportunity_id",
    "strategy_instance_id": "te.trade_id",
}


# Env views a release no longer makes, dropped by name (not left to a CASCADE) so db-init
# removes them on its next run: R3's compatibility view over trade_fill_splits (naming R4).
RETIRED_ENV_VIEWS: tuple[str, ...] = ("instance_allocations",)


def _env_attributed(rows_sql: str) -> str:
    """Wrap a set of raw execution rows with this env's attribution (TD-09, R3).

    ``rows_sql`` selects ``account_executions_id`` plus the canonical columns. Output, in
    the canonical order: IB's ``trade_id`` / ``related_trade_id`` as ``ib_trade_id`` /
    ``ib_related_trade_id``; ``strategy_opportunity_id`` from the trade; in place of the
    Golden Source ``strategy_instance_id``, ``trade_id`` from the whole-fill row of
    ``public.trade_execution`` on (account_id, exec_id). Every name appears once (TD-13).
    """
    cols = [c.strip() for c in _EXEC_CANONICAL_COLS.split(",") if c.strip()]
    out = [_ENV_RENAMED.get(c, f"u.{c}") for c in cols]
    return (
        f"SELECT u.account_executions_id, {', '.join(out)}\n"
        f"        FROM ({rows_sql}) u\n"
        f"        LEFT JOIN public.{TRADE_EXECUTION} te\n"
        "          ON te.account_id = u.account_id AND te.exec_id = u.exec_id\n"
        "         AND te.split_quantity IS NULL\n"
        "        LEFT JOIN public.trade tr ON tr.trade_id = te.trade_id"
    )


def saved_view_grants(cur: Any, schema: str, names: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """The grants on ``schema``'s named views, read before a rebuild drops them.

    ``DROP VIEW`` takes the view's ACL with it, so a rebuild keeps only what the code
    re-grants and the owner's default privileges give back. Roles granted by hand lose
    their access on every db-init: Research's ``analytics_writer`` lost SELECT on
    ``raw_broker.executions_final`` (TD-85 D2) and its memory distill failed on
    2026-10-05. Rows are ``(view, grantee, privilege)``; the owner's own entry is left
    out, PUBLIC is ``'PUBLIC'``.
    """
    cur.execute(
        """
        SELECT c.relname,
               CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END,
               a.privilege_type
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          CROSS JOIN LATERAL aclexplode(c.relacl) a
         WHERE n.nspname = %s AND c.relname = ANY(%s) AND c.relkind = 'v'
           AND a.grantee <> c.relowner
         ORDER BY 1, 2, 3
        """,
        (schema, list(names)),
    )
    return [(row[0], row[1], row[2]) for row in cur.fetchall()]


def restore_view_grants(cur: Any, schema: str, grants: list[tuple[str, str, str]]) -> None:
    """Give back what :func:`saved_view_grants` read, on the views the rebuild recreated.

    A view the rebuild retired is skipped. Grantee names come from ``regrole`` output,
    which quotes them where needed; privileges are ``aclexplode``'s keywords.
    """
    for view, grantee, privilege in grants:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (f"{schema}.{view}",))
        if cur.fetchone()[0]:
            cur.execute(f"GRANT {privilege} ON {schema}.{view} TO {grantee}")


def _create_brokerage_views(cur: Any, schema: str, *, env: bool = False) -> None:
    """The execution views over the three raw tables.

    Golden Source (``env=False``): the attribution columns are the raw tables' own (no
    longer written since TD-09; kept for the rollback window). Per-env DBs (``env=True``,
    over the FDW tables): attribution comes from this env's ``trade_execution``, and two
    env-only views are added -- ``executions_tws`` and ``trade_fill_splits``. Dropping
    ``trade_fill_splits`` CASCADE also drops R3's ``instance_allocations`` where it is left.
    """
    cols = _EXEC_CANONICAL_COLS
    for name in RETIRED_ENV_VIEWS if env else ():
        cur.execute(f"DROP VIEW IF EXISTS {schema}.{name}")
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

    # Split rows, one per raw representation of the fill (Flex id, TWS -id, journal
    # -(1e9+id)), so readers join them by (account_executions_id, account_id).
    cur.execute(
        f"""
        CREATE OR REPLACE VIEW {schema}.trade_fill_splits AS
        SELECT s.account_id, x.account_executions_id, s.trade_id,
               s.split_quantity::double precision AS quantity,
               s.exec_id
        FROM public.{TRADE_EXECUTION} s
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
        WHERE s.split_quantity IS NOT NULL
        """
    )
