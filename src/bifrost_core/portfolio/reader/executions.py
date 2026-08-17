"""Executions and transactions read + performance stats. Uses accounts_helpers; get_performance_stats needs get_accounts_from_tables from accounts."""

import logging
import math
import re
from datetime import date, datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.brokerage_tables import (
    COMMISSIONS,
    CONTRACT_QUOTE_LIVE,
    EXECUTIONS,
    EXECUTIONS_FINAL,
    EXECUTIONS_FLY,
    EXECUTIONS_RAW_TWS,
    INSTANCE_ALLOCATION,
    POSITIONS,
    TRANSACTIONS,
)
from bifrost_core.portfolio.reader.accounts_helpers import (
    _compute_opt_pair_map_and_pairs,
    _compute_opt_realized_calendar,
    _get_current_equity,
    _rows_to_executions,
)

logger = logging.getLogger(__name__)

_CHICAGO = ZoneInfo("America/Chicago")


def _unix_ts_to_chicago_date(ts: float) -> date:
    """Map Unix instant to Chicago calendar date for `trade_date` (DATE) filters."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone(_CHICAGO).date()


# All-source canonical view (Flex > TWS dedup + journal).
_EXEC_READ_TABLE = EXECUTIONS
# Official performance book: Flex + journal only (no TWS gap-fill rows).
_EXEC_FINAL_TABLE = EXECUTIONS_FINAL
# On-the-fly: TWS rows whose (account_id, contract_key) is not in final; excludes BAG (see view DDL).
_EXEC_FLY_TABLE = EXECUTIONS_FLY

# Multi–strategy_instance splits for one execution row (physical bridge table; see DATABASE §2.24.11d).
_EXEC_INST_ALLOC_TABLE = INSTANCE_ALLOCATION

# Raw TWS table (all rows); same canonical columns as account_executions TWS branch, with synthetic id.
_EXEC_TWS_RAW_SUBQUERY = (
    "(SELECT -(executions_raw_tws_id) AS account_executions_id, "
    "account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, "
    "expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, "
    "currency, asset_category, sub_category, description, conid, "
    "security_id, security_id_type, cusip, isin, figi, listing_exchange, "
    "underlying_conid, underlying_symbol, underlying_security_id, underlying_listing_exchange, "
    "issuer, issuer_country_code, trade_id, related_trade_id, report_date, trade_date, "
    "settle_date_target, transaction_type, multiplier, principal_adjust_factor, "
    "proceeds, taxes, net_cash, close_price, open_close_indicator, notes, cost, "
    "fifo_pnl_realized, mtm_pnl, trade_money, fx_rate_to_base, acct_alias, model, "
    "raw_extra, strategy_opportunity_id, strategy_instance_id, created_at "
    f"FROM {EXECUTIONS_RAW_TWS})"
)


def _exec_table_for_scope(source_scope: Optional[str]) -> str:
    """Return the FROM table name for the given source_scope.
    performance_book → final view (flex+journal only, no extra predicate needed).
    on_the_fly → account_executions_fly (TWS not covered by final book).
    tws_raw → subquery over executions_raw_tws (use _exec_from_for_scope).
    all / None → full canonical view.
    """
    s = (source_scope or "").strip().lower()
    if s == "performance_book":
        return _EXEC_FINAL_TABLE
    if s == "on_the_fly":
        return _EXEC_FLY_TABLE
    return _EXEC_READ_TABLE


def _exec_from_for_scope(source_scope: Optional[str]) -> str:
    """FROM … e fragment: either a bare view/table name or a TWS raw subquery."""
    s = (source_scope or "").strip().lower()
    if s == "tws_raw":
        return _EXEC_TWS_RAW_SUBQUERY
    return _exec_table_for_scope(source_scope)


def _source_scope_predicate_e(_source_scope: Optional[str]) -> Optional[str]:
    """No extra WHERE on e.source: scope selects account_executions / _final / _fly view."""
    return None


def _source_scope_sql_fragment(source_scope: Optional[str]) -> str:
    pred = _source_scope_predicate_e(source_scope)
    return f" AND ({pred})" if pred else ""


# Exec_time as UTC epoch (Unix seconds) for API and frontend display; timestamptz stores UTC.
_EXEC_EPOCH_E = "extract(epoch from e.exec_time)"
_EXEC_EPOCH = "extract(epoch from exec_time)"
_CREATED_AT_E = "extract(epoch from e.created_at) AS created_at"
_CREATED_AT = "extract(epoch from created_at) AS created_at"

# Normalize quantity so Sell = negative across sources. tws_client already stores Sell as negative;
# other sources (e.g. flex) store positive for Sell → negate in query for consistent display/aggregation.
_QTY_NORM_E = (
    "CASE WHEN lower(trim(COALESCE(e.source, ''))) = 'tws_client' THEN e.quantity "
    "WHEN upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S') THEN -e.quantity "
    "ELSE e.quantity END AS quantity"
)
_QTY_NORM = (
    "CASE WHEN lower(trim(COALESCE(source, ''))) = 'tws_client' THEN quantity "
    "WHEN upper(trim(COALESCE(side, ''))) IN ('SELL', 'SLD', 'S') THEN -quantity "
    "ELSE quantity END AS quantity"
)

# Normalize commission so "cost" convention is consistent. tws_client stores commission as cost (positive);
# other sources (e.g. flex) may use opposite sign → negate in query when not tws_client.
_COMM_NORM_E = (
    "CASE WHEN lower(trim(COALESCE(e.source, ''))) = 'tws_client' THEN c.commission "
    "WHEN c.commission IS NOT NULL THEN -c.commission ELSE NULL END AS commission"
)

# CommissionReport.realizedPNL first; else Flex fifoPnlRealized on the execution row (executions_raw_*).
_REALIZED_PNL_COALESCE_E = "COALESCE(c.realized_pnl, e.fifo_pnl_realized) AS realized_pnl"


def attach_instance_allocations(conn: Any, executions: List[Dict[str, Any]]) -> None:
    """Populate instance_allocations on each execution dict (mutates in place)."""
    if not conn or not executions:
        return
    ids: List[int] = []
    for e in executions:
        eid = e.get("account_executions_id")
        if eid is not None:
            try:
                ids.append(int(eid))
            except (TypeError, ValueError):
                pass
    if not ids:
        return
    uniq = list(dict.fromkeys(ids))
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT a.account_executions_id, a.strategy_instance_id, a.allocated_quantity,
                       si.label AS strategy_instance_label, si.strategy_opportunity_id
                FROM {_EXEC_INST_ALLOC_TABLE} a
                LEFT JOIN strategy_instance si ON a.strategy_instance_id = si.strategy_instance_id
                WHERE a.account_executions_id = ANY(%s)
                ORDER BY a.strategy_instance_id
                """,
                (uniq,),
            )
            rows = cur.fetchall()
    except Exception as ex:
        if "does not exist" in str(ex).lower() or "42P01" in str(getattr(ex, "pgcode", "")):
            return
        logger.debug("attach_instance_allocations failed: %s", ex)
        return
    by_eid: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        d = dict(r)
        eid = d.get("account_executions_id")
        if eid is None:
            continue
        try:
            ke = int(eid)
        except (TypeError, ValueError):
            continue
        sl = d.get("strategy_instance_label")
        item = {
            "strategy_instance_id": int(d["strategy_instance_id"]),
            "allocated_quantity": float(d["allocated_quantity"]),
            "strategy_opportunity_id": int(d["strategy_opportunity_id"])
            if d.get("strategy_opportunity_id") is not None
            else None,
        }
        if sl is not None and str(sl).strip():
            item["strategy_instance_label"] = str(sl).strip()
        by_eid.setdefault(ke, []).append(item)
    for e in executions:
        eid = e.get("account_executions_id")
        if eid is None:
            continue
        try:
            ke = int(eid)
        except (TypeError, ValueError):
            continue
        if ke in by_eid:
            e["instance_allocations"] = by_eid[ke]


def weight_realized_for_strategy_instance(execution: Dict[str, Any], strategy_instance_id: int) -> float:
    """Fraction of this execution's realized_pnl/commission attributed to strategy_instance_id (0..1)."""
    try:
        sid = int(strategy_instance_id)
    except (TypeError, ValueError):
        return 0.0
    allocs = execution.get("instance_allocations") or []
    if allocs:
        denom = 0.0
        for a in allocs:
            try:
                denom += abs(float(a.get("allocated_quantity") or 0))
            except (TypeError, ValueError):
                pass
        if denom <= 0:
            return 0.0
        for a in allocs:
            try:
                if int(a.get("strategy_instance_id")) == sid:
                    return abs(float(a.get("allocated_quantity") or 0)) / denom
            except (TypeError, ValueError):
                continue
        return 0.0
    si = execution.get("strategy_instance_id")
    if si is not None and int(si) == sid:
        return 1.0
    return 0.0


def _add_realized_splits_to_opp_and_inst(
    e: Dict[str, Any],
    by_opp: Dict[int, Dict[str, Any]],
    by_inst: Dict[int, Dict[str, Any]],
    only_strategy_instance_id: Optional[int] = None,
) -> None:
    """Add one execution's realized_pnl/commission into by_opp and by_inst (handles instance_allocations).

    When only_strategy_instance_id is set (Performance filter), attribute only to that instance.
    """
    rp_val = float(e["realized_pnl"]) if e.get("realized_pnl") is not None else 0.0
    comm_val = float(e["commission"]) if e.get("commission") is not None else 0.0
    if not math.isfinite(rp_val):
        rp_val = 0.0
    if not math.isfinite(comm_val):
        comm_val = 0.0
    raw_allocs = e.get("instance_allocations") or []
    full_denom = 0.0
    for a in raw_allocs:
        try:
            full_denom += abs(float(a.get("allocated_quantity") or 0))
        except (TypeError, ValueError):
            pass
    work_allocs = raw_allocs
    if only_strategy_instance_id is not None and raw_allocs:
        sid_f = int(only_strategy_instance_id)
        work_allocs = [a for a in raw_allocs if int(a.get("strategy_instance_id") or -1) == sid_f]
    if only_strategy_instance_id is not None and raw_allocs and not work_allocs:
        return
    if work_allocs:
        denom = full_denom if full_denom > 0 else 0.0
        if denom <= 0:
            return
        opp_trade_bump: set = set()
        for a in work_allocs:
            try:
                w = abs(float(a.get("allocated_quantity") or 0)) / denom
                si_id = int(a["strategy_instance_id"])
                so_id = a.get("strategy_opportunity_id")
            except (TypeError, ValueError, KeyError):
                continue
            rp_part = rp_val * w
            comm_part = comm_val * w
            if so_id is not None:
                so_id = int(so_id)
                if so_id not in by_opp:
                    by_opp[so_id] = {
                        "strategy_opportunity_id": so_id,
                        "total_pnl": 0.0,
                        "commission": 0.0,
                        "net_pnl": 0.0,
                        "trade_count": 0,
                    }
                by_opp[so_id]["total_pnl"] += rp_part
                by_opp[so_id]["commission"] += comm_part
                by_opp[so_id]["net_pnl"] += rp_part - comm_part
                if so_id not in opp_trade_bump:
                    by_opp[so_id]["trade_count"] += 1
                    opp_trade_bump.add(so_id)
            if si_id not in by_inst:
                by_inst[si_id] = {
                    "strategy_instance_id": si_id,
                    "total_pnl": 0.0,
                    "commission": 0.0,
                    "net_pnl": 0.0,
                    "trade_count": 0,
                }
            by_inst[si_id]["total_pnl"] += rp_part
            by_inst[si_id]["commission"] += comm_part
            by_inst[si_id]["net_pnl"] += rp_part - comm_part
            by_inst[si_id]["trade_count"] += 1
        return
    so_id = e.get("strategy_opportunity_id")
    si_id = e.get("strategy_instance_id")
    if only_strategy_instance_id is not None:
        if si_id is None or int(si_id) != int(only_strategy_instance_id):
            return
    if so_id is not None:
        so_id = int(so_id)
        if so_id not in by_opp:
            by_opp[so_id] = {
                "strategy_opportunity_id": so_id,
                "total_pnl": 0.0,
                "commission": 0.0,
                "net_pnl": 0.0,
                "trade_count": 0,
            }
        by_opp[so_id]["total_pnl"] += rp_val
        by_opp[so_id]["commission"] += comm_val
        by_opp[so_id]["net_pnl"] += rp_val - comm_val
        by_opp[so_id]["trade_count"] += 1
    if si_id is not None:
        si_id = int(si_id)
        if si_id not in by_inst:
            by_inst[si_id] = {
                "strategy_instance_id": si_id,
                "total_pnl": 0.0,
                "commission": 0.0,
                "net_pnl": 0.0,
                "trade_count": 0,
            }
        by_inst[si_id]["total_pnl"] += rp_val
        by_inst[si_id]["commission"] += comm_val
        by_inst[si_id]["net_pnl"] += rp_val - comm_val
        by_inst[si_id]["trade_count"] += 1


def _qty_expr_e_for_scope(source_scope: Optional[str]) -> str:
    """Quantity column for get_executions FROM clause alias `e`.

    executions_raw_tws stores quantity as unsigned (positive); direction is in `side`.
    Do not apply Flex-style Sell→negative normalization for source_scope=tws_raw.
    """
    if (source_scope or "").strip().lower() == "tws_raw":
        return "e.quantity AS quantity"
    return _QTY_NORM_E


def get_executions(
    conn: Any,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    account_id: Optional[str] = None,
    limit: Optional[int] = 200,
    strategy_opportunity_id: Optional[int] = None,
    strategy_instance_id: Optional[int] = None,
    source_scope: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if conn is None:
        return []
    try:
        conditions = []
        values: List[Any] = []
        if since_ts is not None:
            conditions.append("e.trade_date >= %s")
            values.append(_unix_ts_to_chicago_date(since_ts))
        if until_ts is not None:
            conditions.append("e.trade_date <= %s")
            values.append(_unix_ts_to_chicago_date(until_ts))
        if account_id is not None and account_id.strip():
            conditions.append("e.account_id = %s")
            values.append(account_id.strip())
        if strategy_opportunity_id is not None:
            conditions.append("e.strategy_opportunity_id = %s")
            values.append(strategy_opportunity_id)
        if strategy_instance_id is not None:
            conditions.append(
                f"(e.strategy_instance_id = %s OR EXISTS (SELECT 1 FROM {_EXEC_INST_ALLOC_TABLE} a "
                f"WHERE a.account_executions_id = e.account_executions_id AND "
                f"a.account_id IS NOT DISTINCT FROM e.account_id AND a.strategy_instance_id = %s))"
            )
            values.append(strategy_instance_id)
            values.append(strategy_instance_id)
        pred_e = _source_scope_predicate_e(source_scope)
        if pred_e:
            conditions.append(pred_e)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        use_limit = limit is not None and limit > 0
        if use_limit:
            values.append(limit)
        limit_clause = " LIMIT %s" if use_limit else ""
        from_table = _exec_from_for_scope(source_scope)
        _qty_e = _qty_expr_e_for_scope(source_scope)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(
                    f"""
                    SELECT e.account_executions_id, e.account_id, e.exec_id, {_EXEC_EPOCH_E} AS time,
                           e.symbol, e.sec_type, e.side, {_qty_e}, e.price,
                           {_COMM_NORM_E}, e.source,
                           e.expiry, e.strike, e.option_right, e.exchange, e.order_id, e.cum_qty,
                           {_REALIZED_PNL_COALESCE_E}, e.contract_key, c.currency, c.yield_, c.yield_redemption_date,
                           e.trade_date, e.report_date, e.settle_date_target, e.transaction_type, e.taxes, e.net_cash,
                           e.raw_extra, {_CREATED_AT_E},
                           e.strategy_opportunity_id, e.strategy_instance_id,
                           so.name AS strategy_opportunity_name, si.label AS strategy_instance_label,
                           EXTRACT(EPOCH FROM si.opened_at)::bigint AS strategy_instance_opened_at_epoch
                    FROM {from_table} e
                    LEFT JOIN {COMMISSIONS} c ON e.exec_id = c.exec_id AND e.exec_id IS NOT NULL
                    LEFT JOIN strategy_opportunity so ON e.strategy_opportunity_id = so.strategy_opportunity_id
                    LEFT JOIN strategy_instance si ON e.strategy_instance_id = si.strategy_instance_id
                    {where}
                    ORDER BY e.trade_date DESC NULLS LAST, e.exec_time DESC NULLS LAST{limit_clause}
                    """,
                    values,
                )
            except Exception as col_err:
                if "does not exist" in str(col_err).lower() or "42703" in str(getattr(col_err, "pgcode", "")):
                    try:
                        cur.execute(
                            f"""
                            SELECT e.account_executions_id, e.account_id, e.exec_id, {_EXEC_EPOCH_E} AS time,
                                   e.symbol, e.sec_type, e.side, {_qty_e}, e.price,
                                   {_COMM_NORM_E}, e.source,
                                   e.expiry, e.strike, e.option_right, e.exchange, e.order_id, e.cum_qty,
                                   {_REALIZED_PNL_COALESCE_E}, e.contract_key, c.currency, c.yield_, c.yield_redemption_date,
                                   e.trade_date, e.raw_extra, {_CREATED_AT_E}
                            FROM {from_table} e
                            LEFT JOIN {COMMISSIONS} c ON e.exec_id = c.exec_id
                            {where}
                            ORDER BY e.trade_date DESC NULLS LAST, e.exec_time DESC NULLS LAST{limit_clause}
                            """,
                            values,
                        )
                    except Exception:
                        cur.execute(
                            f"""
                                    SELECT e.account_executions_id, e.account_id, e.exec_id, {_EXEC_EPOCH_E} AS time,
                                           e.symbol, e.sec_type, e.side, {_qty_e}, e.price,
                                           NULL::double precision AS commission, e.source,
                                           e.expiry, e.strike, e.option_right, e.exchange, e.order_id, e.cum_qty,
                                           e.fifo_pnl_realized AS realized_pnl, e.contract_key,
                                           NULL::text AS currency, NULL::double precision AS yield_, NULL::integer AS yield_redemption_date,
                                           e.trade_date, e.raw_extra, {_CREATED_AT_E}
                                    FROM {from_table} e
                                    {where}
                            ORDER BY e.trade_date DESC NULLS LAST, e.exec_time DESC NULLS LAST{limit_clause}
                            """,
                            values,
                        )
                else:
                    raise
            rows = cur.fetchall()
        out = _rows_to_executions(rows, None)
        attach_instance_allocations(conn, out)
        return out
    except Exception as e:
        logger.debug("get_executions failed: %s", e)
        return []


def get_executions_freshness(conn: Any) -> List[Dict[str, Any]]:
    if conn is None:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    account_id,
                    source,
                    extract(epoch from max(exec_time)) AS latest_exec_ts,
                    extract(epoch from (now() - max(exec_time))) / 86400.0 AS days_since_latest
                FROM {_EXEC_READ_TABLE}
                WHERE exec_time IS NOT NULL
                GROUP BY account_id, source
                ORDER BY account_id, source
                """
            )
            rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            ts_val = d.get("latest_exec_ts")
            days_val = d.get("days_since_latest")
            try:
                d["latest_exec_ts"] = float(ts_val) if ts_val is not None else None
            except (TypeError, ValueError):
                d["latest_exec_ts"] = None
            try:
                d["days_since_latest"] = float(days_val) if days_val is not None else None
            except (TypeError, ValueError):
                d["days_since_latest"] = None
            out.append(d)
        return out
    except Exception as e:
        logger.debug("get_executions_freshness failed: %s", e)
        return []


def get_executions_by_contract_keys(
    conn: Any,
    contract_keys: List[Tuple[str, str, str, str]],
    account_id: Optional[str] = None,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    if not contract_keys or conn is None:
        return []
    keys_dedup = list(dict.fromkeys(contract_keys))
    placeholders = ",".join(["(%s,%s,%s,%s)"] * len(keys_dedup))
    values: List[Any] = []
    for (sym, exp, strike_s, acc) in keys_dedup:
        values.extend([sym, exp, strike_s, acc])
    conditions = [
        f"(e.symbol, e.expiry, COALESCE(e.strike::text,''), e.account_id) IN ({placeholders})",
        "upper(trim(COALESCE(e.sec_type,''))) = 'OPT'",
    ]
    if account_id is not None and account_id.strip():
        conditions.append("e.account_id = %s")
        values.append(account_id.strip())
    where = " AND ".join(conditions)
    values.append(limit)
    sql = f"""
                    SELECT e.account_executions_id, e.account_id, e.exec_id, {_EXEC_EPOCH_E} AS time,
                           e.symbol, e.sec_type, e.side, {_QTY_NORM_E}, e.price,
                           {_COMM_NORM_E}, e.source,
                           e.expiry, e.strike, e.option_right, e.exchange, e.order_id, e.cum_qty,
                           {_REALIZED_PNL_COALESCE_E}, e.contract_key, c.currency, c.yield_, c.yield_redemption_date,
                           e.trade_date, e.report_date, e.settle_date_target, e.transaction_type, e.taxes, e.net_cash,
                           e.raw_extra, {_CREATED_AT_E}
                    FROM {_EXEC_READ_TABLE} e
                    LEFT JOIN {COMMISSIONS} c ON e.exec_id = c.exec_id AND e.exec_id IS NOT NULL
                    WHERE {where}
                    ORDER BY e.trade_date ASC NULLS LAST, e.exec_time ASC NULLS LAST
                    LIMIT %s
                    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(sql, values)
            except Exception as col_err:
                if "does not exist" in str(col_err).lower() or "42703" in str(getattr(col_err, "pgcode", "")):
                    try:
                        cur.execute(
                            f"""
                            SELECT e.account_executions_id, e.account_id, e.exec_id, {_EXEC_EPOCH_E} AS time,
                                   e.symbol, e.sec_type, e.side, {_QTY_NORM_E}, e.price,
                                   {_COMM_NORM_E}, e.source,
                                   e.expiry, e.strike, e.option_right, e.exchange, e.order_id, e.cum_qty,
                                   {_REALIZED_PNL_COALESCE_E}, e.contract_key, c.currency, c.yield_, c.yield_redemption_date,
                                   e.trade_date, e.raw_extra, {_CREATED_AT_E}
                            FROM {_EXEC_READ_TABLE} e
                            LEFT JOIN {COMMISSIONS} c ON e.exec_id = c.exec_id
                            WHERE {where}
                            ORDER BY e.trade_date ASC NULLS LAST, e.exec_time ASC NULLS LAST
                            LIMIT %s
                            """,
                            values,
                        )
                    except Exception:
                        vals_no_acc = list(values)
                        cur.execute(
                            f"""
                            SELECT account_executions_id, account_id, exec_id, {_EXEC_EPOCH} AS time,
                                   symbol, sec_type, side, {_QTY_NORM}, price,
                                   NULL::double precision AS commission, source,
                                   expiry, strike, option_right, exchange, order_id, cum_qty,
                                   fifo_pnl_realized AS realized_pnl, contract_key,
                                   NULL::text AS currency, NULL::double precision AS yield_, NULL::integer AS yield_redemption_date,
                                   trade_date, raw_extra, {_CREATED_AT}
                            FROM {_EXEC_READ_TABLE}
                            WHERE (symbol, expiry, COALESCE(strike::text,''), account_id) IN ({placeholders})
                              AND upper(trim(COALESCE(sec_type,''))) = 'OPT'
                            ORDER BY trade_date ASC NULLS LAST, exec_time ASC NULLS LAST
                            LIMIT %s
                            """,
                            vals_no_acc,
                        )
                else:
                    raise
            rows = cur.fetchall()
        return _rows_to_executions(rows, None)
    except Exception as e:
        logger.debug("get_executions_by_contract_keys failed: %s", e)
        return []


def _occ_local_symbol(symbol: str, expiry_yyyymmdd: str, strike: float, right: str) -> str:
    """IB/OCC-style local symbol root: 6-char root (space-pad) + YYMMDD + C/P + strike*1000 (8 digits)."""
    exp = re.sub(r"\D", "", expiry_yyyymmdd or "")
    if len(exp) >= 8:
        yymmdd = exp[2:8]
    elif len(exp) == 6:
        yymmdd = exp
    else:
        yymmdd = (exp + "010101")[:6]
    root = (symbol or "").strip().upper()[:6].ljust(6)
    r = (right or "C").strip().upper()[:1]
    if r not in ("C", "P"):
        r = "C"
    strike_milli = int(round(float(strike) * 1000))
    strike_milli = max(0, min(strike_milli, 99999999))
    return f"{root}{yymmdd}{r}{strike_milli:08d}"


def _contract_key_variants_position_vs_executions(contract_key: str) -> List[str]:
    """
    account_positions uses SYMBOL|OPT|YYYYMMDD|strike|C/P (e.g. RKLB|OPT|20260320|80.0|C).
    account_executions often uses OCC local|OPT|YYYYMMDD|strike|C/P
    (e.g. RKLB  260320C00080000|OPT|20260320|80.0|C).
    """
    ck = (contract_key or "").strip()
    parts = ck.split("|")
    if len(parts) < 5 or parts[1].strip().upper() != "OPT":
        return [ck]
    sym_seg, exp_raw, strike_raw, right_raw = parts[0], parts[2], parts[3], parts[4]
    # Execution-style OCC local (positions use short symbol, e.g. RKLB vs RKLB  260320C00080000)
    if len(sym_seg) > 6:
        return [ck]
    try:
        strike_f = float(strike_raw)
    except (TypeError, ValueError):
        return [ck]
    r = right_raw.strip().upper()[:1]
    if r not in ("C", "P"):
        r = "C"
    exp_digits = re.sub(r"\D", "", exp_raw)
    exp8 = exp_digits[:8] if len(exp_digits) >= 8 else exp_digits.ljust(8, "0")[:8]
    sym = re.split(r"\s+", sym_seg.strip())[0].upper()[:6]
    occ = _occ_local_symbol(sym, exp8, strike_f, r)
    tails = list(
        dict.fromkeys(
            [
                strike_raw.strip(),
                str(int(strike_f)) if strike_f == int(strike_f) else strike_raw,
                f"{strike_f:.1f}",
                f"{strike_f:g}",
            ]
        )
    )
    keys: List[str] = [ck, f"{occ}|OPT|{exp8}|{strike_raw.strip()}|{r}"]
    for t in tails:
        keys.append(f"{occ}|OPT|{exp8}|{t}|{r}")
    return list(dict.fromkeys(keys))


def _leg_match_tuples(account_id: str, symbol: str, expiry_raw: str, strike_val: Any) -> List[Tuple[str, str, str, str]]:
    sym = (symbol or "").strip()
    acc = (account_id or "").strip()
    if not sym or not acc:
        return []
    exp_digits = re.sub(r"\D", "", str(expiry_raw or ""))
    exps: set[str] = set()
    if len(exp_digits) >= 8:
        exps.add(exp_digits[:8])
        exps.add(exp_digits[:6])
    elif len(exp_digits) == 6:
        exps.add(exp_digits)
    elif exp_digits:
        exps.add(exp_digits)
    else:
        return []
    try:
        strike_f = float(strike_val)
    except (TypeError, ValueError):
        return []
    strikes: set[str] = set()
    strikes.add(str(int(strike_f)) if strike_f == int(strike_f) else str(strike_f))
    strikes.add(f"{strike_f:.1f}")
    if strike_f == int(strike_f):
        strikes.add(f"{int(strike_f)}.0")
        strikes.add(str(float(int(strike_f))))
    keys = [(sym, e, s, acc) for e in exps for s in strikes]
    return list(dict.fromkeys(keys))


def get_executions_for_strategy_link(
    conn: Any,
    account_id: str,
    contract_key: Optional[str] = None,
    symbol: Optional[str] = None,
    expiry: Optional[str] = None,
    strike: Optional[Any] = None,
    option_right: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Candidates to attach strategy_opportunity_id / strategy_instance_id (no insert). By contract_key first, else symbol+expiry+strike."""
    if conn is None:
        return []
    acc = (account_id or "").strip()
    if not acc:
        return []
    lim = max(1, min(int(limit or 200), 500))

    def _run_sql(where_sql: str, params: List[Any]) -> List[Dict[str, Any]]:
        vals = list(params) + [lim]
        sql = f"""
                    SELECT e.account_executions_id, e.account_id, e.exec_id, {_EXEC_EPOCH_E} AS time,
                           e.symbol, e.sec_type, e.side, {_QTY_NORM_E}, e.price,
                           {_COMM_NORM_E}, e.source,
                           e.expiry, e.strike, e.option_right, e.exchange, e.order_id, e.cum_qty,
                           {_REALIZED_PNL_COALESCE_E}, e.contract_key, c.currency, c.yield_, c.yield_redemption_date,
                           e.trade_date, e.report_date, e.settle_date_target, e.transaction_type, e.taxes, e.net_cash,
                           e.raw_extra, {_CREATED_AT_E},
                           e.strategy_opportunity_id, e.strategy_instance_id,
                           so.name AS strategy_opportunity_name, si.label AS strategy_instance_label,
                           EXTRACT(EPOCH FROM si.opened_at)::bigint AS strategy_instance_opened_at_epoch
                    FROM {_EXEC_READ_TABLE} e
                    LEFT JOIN {COMMISSIONS} c ON e.exec_id = c.exec_id AND e.exec_id IS NOT NULL
                    LEFT JOIN strategy_opportunity so ON e.strategy_opportunity_id = so.strategy_opportunity_id
                    LEFT JOIN strategy_instance si ON e.strategy_instance_id = si.strategy_instance_id
                    WHERE {where_sql}
                    ORDER BY e.exec_time DESC NULLS LAST
                    LIMIT %s
                    """
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, vals)
                rows = cur.fetchall()
            return _rows_to_executions(rows, None)
        except Exception as ex:
            if "does not exist" in str(ex).lower() or "42703" in str(getattr(ex, "pgcode", "")):
                try:
                    sql2 = f"""
                            SELECT e.account_executions_id, e.account_id, e.exec_id, {_EXEC_EPOCH_E} AS time,
                                   e.symbol, e.sec_type, e.side, {_QTY_NORM_E}, e.price,
                                   {_COMM_NORM_E}, e.source,
                                   e.expiry, e.strike, e.option_right, e.exchange, e.order_id, e.cum_qty,
                                   {_REALIZED_PNL_COALESCE_E}, e.contract_key, c.currency, c.yield_, c.yield_redemption_date,
                                   e.trade_date, e.raw_extra, {_CREATED_AT_E},
                                   e.strategy_opportunity_id, e.strategy_instance_id,
                                   so.name AS strategy_opportunity_name, si.label AS strategy_instance_label,
                                   EXTRACT(EPOCH FROM si.opened_at)::bigint AS strategy_instance_opened_at_epoch
                            FROM {_EXEC_READ_TABLE} e
                            LEFT JOIN {COMMISSIONS} c ON e.exec_id = c.exec_id
                            LEFT JOIN strategy_opportunity so ON e.strategy_opportunity_id = so.strategy_opportunity_id
                            LEFT JOIN strategy_instance si ON e.strategy_instance_id = si.strategy_instance_id
                            WHERE {where_sql}
                            ORDER BY e.exec_time DESC NULLS LAST
                            LIMIT %s
                            """
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(sql2, vals)
                        rows = cur.fetchall()
                    return _rows_to_executions(rows, None)
                except Exception:
                    logger.debug("get_executions_for_strategy_link fallback: %s", ex)
                    return []
            logger.debug("get_executions_for_strategy_link: %s", ex)
            return []

    rows: List[Dict[str, Any]] = []
    ck = (contract_key or "").strip()
    if ck:
        key_variants = _contract_key_variants_position_vs_executions(ck)
        if len(key_variants) == 1:
            rows = _run_sql("e.account_id = %s AND e.contract_key = %s", [acc, key_variants[0]])
        else:
            ph = ",".join(["%s"] * len(key_variants))
            rows = _run_sql(
                f"e.account_id = %s AND e.contract_key IN ({ph})",
                [acc] + key_variants,
            )
    if not rows and symbol and expiry is not None and strike is not None:
        tuples = _leg_match_tuples(acc, symbol, str(expiry), strike)
        if tuples:
            ph = ",".join(["(%s,%s,%s,%s)"] * len(tuples))
            flat: List[Any] = []
            for t in tuples:
                flat.extend(t)
            where_leg = (
                f"(e.symbol, e.expiry, COALESCE(e.strike::text,''), e.account_id) IN ({ph}) "
                "AND upper(trim(COALESCE(e.sec_type,''))) = 'OPT'"
            )
            rows = _run_sql(where_leg, flat)

    r0 = (option_right or "").strip().upper()[:1]
    if r0 in ("C", "P") and rows:
        rows = [
            x
            for x in rows
            if str((x.get("option_right") or "")).strip().upper().startswith(r0)
        ]

    seen: Dict[Any, Dict[str, Any]] = {}
    for x in rows:
        eid = x.get("account_executions_id")
        if eid is not None and eid not in seen:
            seen[eid] = x
    out = list(seen.values())
    out.sort(key=lambda z: float(z.get("time") or 0), reverse=True)
    return out[:lim]


def get_executions_with_opt_pairs(
    conn: Any,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    account_id: Optional[str] = None,
    limit: int = 200,
    strategy_opportunity_id: Optional[int] = None,
    strategy_instance_id: Optional[int] = None,
    source_scope: Optional[str] = None,
) -> Dict[str, Any]:
    day_executions = get_executions(
        conn,
        since_ts=since_ts,
        until_ts=until_ts,
        account_id=account_id,
        limit=limit,
        strategy_opportunity_id=strategy_opportunity_id,
        strategy_instance_id=strategy_instance_id,
        source_scope=source_scope,
    )
    if since_ts is None or until_ts is None:
        for e in day_executions:
            e["paired_execution_ids"] = []
        return {"executions": day_executions, "opt_pairs": []}
    all_legs = get_executions_with_opt_pairs_single_query(
        conn,
        since_ts=since_ts,
        until_ts=until_ts,
        account_id=account_id,
        limit=5000,
        strategy_opportunity_id=strategy_opportunity_id,
        strategy_instance_id=strategy_instance_id,
        source_scope=source_scope,
    )
    pair_map, opt_pairs = _compute_opt_pair_map_and_pairs(all_legs)
    try:
        from zoneinfo import ZoneInfo
        chicago = ZoneInfo("America/Chicago")
        since_date = datetime.fromtimestamp(since_ts, tz=timezone.utc).astimezone(chicago).date()
        until_date = datetime.fromtimestamp(until_ts, tz=timezone.utc).astimezone(chicago).date()
    except Exception:
        since_date = until_date = None
    id_to_trade_date: Dict[int, date] = {}
    for leg in all_legs:
        eid = leg.get("account_executions_id")
        if eid is None:
            continue
        td = leg.get("trade_date")
        if td is not None:
            if isinstance(td, date):
                id_to_trade_date[int(eid)] = td
            elif isinstance(td, str) and len(td) >= 10:
                try:
                    id_to_trade_date[int(eid)] = datetime.strptime(td[:10], "%Y-%m-%d").date()
                except (TypeError, ValueError):
                    pass
        if int(eid) not in id_to_trade_date:
            t = leg.get("time")
            if t is not None:
                try:
                    from zoneinfo import ZoneInfo
                    ts = float(t)
                    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ZoneInfo("America/Chicago"))
                    id_to_trade_date[int(eid)] = dt.date()
                except Exception:
                    pass
    filtered_pairs = []
    for p in opt_pairs:
        cid = p.get("leg_c_execution_id")
        pid = p.get("leg_p_execution_id")
        dc = id_to_trade_date.get(int(cid)) if cid is not None else None
        dp = id_to_trade_date.get(int(pid)) if pid is not None else None
        if dc is None or dp is None:
            continue
        if since_date is not None and until_date is not None and (dc < since_date or dc > until_date or dp < since_date or dp > until_date):
            continue
        filtered_pairs.append(p)
    pair_map_filtered: Dict[int, List[int]] = {}
    for p in filtered_pairs:
        aid = p.get("leg_c_execution_id")
        bid = p.get("leg_p_execution_id")
        if aid is not None and bid is not None:
            if aid not in pair_map_filtered:
                pair_map_filtered[aid] = []
            if bid not in pair_map_filtered[aid]:
                pair_map_filtered[aid].append(bid)
            if bid not in pair_map_filtered:
                pair_map_filtered[bid] = []
            if aid not in pair_map_filtered[bid]:
                pair_map_filtered[bid].append(aid)
    for e in day_executions:
        eid = e.get("account_executions_id")
        if eid is not None:
            e["paired_execution_ids"] = pair_map_filtered.get(int(eid), [])
        else:
            e["paired_execution_ids"] = []
    return {"executions": day_executions, "opt_pairs": filtered_pairs}


def get_executions_with_opt_pairs_single_query(
    conn: Any,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    account_id: Optional[str] = None,
    limit: int = 5000,
    strategy_opportunity_id: Optional[int] = None,
    strategy_instance_id: Optional[int] = None,
    source_scope: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if since_ts is None or until_ts is None or conn is None:
        return []
    since_d = _unix_ts_to_chicago_date(since_ts)
    until_d = _unix_ts_to_chicago_date(until_ts)
    values: List[Any] = [since_d, until_d]
    acc_cond = ""
    if account_id and account_id.strip():
        acc_cond = " AND e.account_id = %s"
        values.append(account_id.strip())
    strat_cond = ""
    if strategy_opportunity_id is not None:
        strat_cond += " AND e.strategy_opportunity_id = %s"
        values.append(strategy_opportunity_id)
    if strategy_instance_id is not None:
        strat_cond += (
            f" AND (e.strategy_instance_id = %s OR EXISTS (SELECT 1 FROM {_EXEC_INST_ALLOC_TABLE} a "
            f"WHERE a.account_executions_id = e.account_executions_id AND "
            f"a.account_id IS NOT DISTINCT FROM e.account_id AND a.strategy_instance_id = %s))"
        )
        values.append(strategy_instance_id)
        values.append(strategy_instance_id)
    src_frag = _source_scope_sql_fragment(source_scope)
    from_table = _exec_from_for_scope(source_scope)
    values2: List[Any] = [since_d, until_d, since_d, until_d]
    if account_id and account_id.strip():
        values2.append(account_id.strip())
    if strategy_opportunity_id is not None:
        values2.append(strategy_opportunity_id)
    if strategy_instance_id is not None:
        values2.append(strategy_instance_id)
        values2.append(strategy_instance_id)
    values2.append(limit)
    sql = f"""
WITH day_keys AS (
  SELECT DISTINCT e.symbol, e.expiry, COALESCE(e.strike::text,'') AS strike_s, e.account_id
  FROM {from_table} e
  WHERE e.trade_date >= %s
    AND e.trade_date <= %s
    AND upper(trim(COALESCE(e.sec_type,''))) = 'OPT'
    {acc_cond}
    {strat_cond}
    {src_frag}
),
all_legs AS (
  SELECT e.account_executions_id, e.account_id, e.exec_id, {_EXEC_EPOCH_E} AS time,
         e.trade_date,
         e.symbol, e.sec_type, e.side, {_QTY_NORM_E}, e.price,
         {_COMM_NORM_E}, e.source,
         e.expiry, e.strike, e.option_right, e.exchange, e.order_id, e.cum_qty,
         {_REALIZED_PNL_COALESCE_E}, e.contract_key, c.currency, c.yield_, c.yield_redemption_date, e.raw_extra,
         {_CREATED_AT_E},
         (e.trade_date >= %s AND e.trade_date <= %s) AS in_selected_day,
         upper(trim(COALESCE(e.side,''))) AS side_norm
  FROM {from_table} e
  INNER JOIN day_keys k ON e.symbol = k.symbol AND e.expiry = k.expiry
    AND COALESCE(e.strike::text,'') = k.strike_s AND e.account_id = k.account_id
  LEFT JOIN {COMMISSIONS} c ON e.exec_id = c.exec_id AND e.exec_id IS NOT NULL
  WHERE upper(trim(COALESCE(e.sec_type,''))) = 'OPT'
    AND e.trade_date >= %s
    AND e.trade_date <= %s
    {acc_cond}
    {strat_cond}
    {src_frag}
),
numbered AS (
  SELECT all_legs.*,
         ROW_NUMBER() OVER (PARTITION BY symbol, expiry, strike, account_id, side_norm ORDER BY time ASC NULLS LAST) AS opt_pair_rn
  FROM all_legs
  WHERE side_norm IN ('BUY', 'SELL')
)
SELECT * FROM numbered ORDER BY time ASC NULLS LAST LIMIT %s
"""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(sql, values + values2)
            except Exception as col_err:
                if "does not exist" in str(col_err).lower() or "42703" in str(getattr(col_err, "pgcode", "")):
                    values_fb: List[Any] = [since_d, until_d]
                    if account_id and account_id.strip():
                        values_fb.append(account_id.strip())
                    if strategy_opportunity_id is not None:
                        values_fb.append(strategy_opportunity_id)
                    if strategy_instance_id is not None:
                        values_fb.append(strategy_instance_id)
                        values_fb.append(strategy_instance_id)
                    values2_fb: List[Any] = [since_d, until_d, since_d, until_d]
                    if account_id and account_id.strip():
                        values2_fb.append(account_id.strip())
                    if strategy_opportunity_id is not None:
                        values2_fb.append(strategy_opportunity_id)
                    if strategy_instance_id is not None:
                        values2_fb.append(strategy_instance_id)
                        values2_fb.append(strategy_instance_id)
                    values2_fb.append(limit)
                    sql_fallback = f"""
WITH day_keys AS (
  SELECT DISTINCT e.symbol, e.expiry, COALESCE(e.strike::text,'') AS strike_s, e.account_id
  FROM {from_table} e
  WHERE e.trade_date >= %s
    AND e.trade_date <= %s
    AND upper(trim(COALESCE(e.sec_type,''))) = 'OPT'
    {acc_cond}
    {strat_cond}
    {src_frag}
),
all_legs AS (
  SELECT e.account_executions_id, e.account_id, e.exec_id, {_EXEC_EPOCH_E} AS time,
         e.trade_date,
         e.symbol, e.sec_type, e.side, {_QTY_NORM_E}, e.price,
         NULL::double precision AS commission, e.source,
         e.expiry, e.strike, e.option_right, e.exchange, e.order_id, e.cum_qty,
         {_REALIZED_PNL_COALESCE_E}, e.contract_key, NULL::text AS currency,
         NULL::double precision AS yield_, NULL::integer AS yield_redemption_date, e.raw_extra,
         {_CREATED_AT_E},
         (e.trade_date >= %s AND e.trade_date <= %s) AS in_selected_day,
         upper(trim(COALESCE(e.side,''))) AS side_norm
  FROM {from_table} e
  INNER JOIN day_keys k ON e.symbol = k.symbol AND e.expiry = k.expiry
    AND COALESCE(e.strike::text,'') = k.strike_s AND e.account_id = k.account_id
  LEFT JOIN {COMMISSIONS} c ON e.exec_id = c.exec_id AND e.exec_id IS NOT NULL
  WHERE upper(trim(COALESCE(e.sec_type,''))) = 'OPT'
    AND e.trade_date >= %s
    AND e.trade_date <= %s
    {acc_cond}
    {strat_cond}
    {src_frag}
),
numbered AS (
  SELECT all_legs.*,
         ROW_NUMBER() OVER (PARTITION BY symbol, expiry, strike, account_id, side_norm ORDER BY time ASC NULLS LAST) AS opt_pair_rn
  FROM all_legs
  WHERE side_norm IN ('BUY', 'SELL')
)
SELECT * FROM numbered ORDER BY time ASC NULLS LAST LIMIT %s
"""
                    cur.execute(sql_fallback, values_fb + values2_fb)
                else:
                    raise
            rows = cur.fetchall()
        out = _rows_to_executions(rows, None)
        attach_instance_allocations(conn, out)
        return out
    except Exception as e:
        logger.debug("get_executions_with_opt_pairs_single_query failed: %s", e)
        return []


def get_net_cash_flow(
    conn: Any,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    account_id: Optional[str] = None,
) -> float:
    if conn is None:
        return 0.0
    try:
        with conn.cursor() as cur:
            q = f"SELECT COALESCE(SUM(amount), 0) AS total FROM {TRANSACTIONS} WHERE 1=1"
            args: List[Any] = []
            if since_ts is not None:
                q += " AND ts >= to_timestamp(%s)"
                args.append(since_ts)
            if until_ts is not None:
                q += " AND ts <= to_timestamp(%s)"
                args.append(until_ts)
            if account_id is not None and str(account_id).strip():
                q += " AND account_id = %s"
                args.append(str(account_id).strip())
            cur.execute(q, args)
            row = cur.fetchone()
        if row and row[0] is not None:
            v = float(row[0])
            return v if math.isfinite(v) else 0.0
        return 0.0
    except Exception as e:
        logger.debug("get_net_cash_flow failed: %s", e)
        return 0.0


def get_transactions(
    conn: Any,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    account_id: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    if conn is None:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            q = f"""
                SELECT account_transactions_id, account_id, extract(epoch from ts) AS ts, amount, type, currency, description, created_at
                FROM {TRANSACTIONS} WHERE 1=1
            """
            args: List[Any] = []
            if since_ts is not None:
                q += " AND ts >= to_timestamp(%s)"
                args.append(since_ts)
            if until_ts is not None:
                q += " AND ts <= to_timestamp(%s)"
                args.append(until_ts)
            if account_id is not None and str(account_id).strip():
                q += " AND account_id = %s"
                args.append(str(account_id).strip())
            q += " ORDER BY ts DESC LIMIT %s"
            args.append(limit)
            cur.execute(q, args)
            rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception as e:
        logger.debug("get_transactions failed: %s", e)
        return []


def _performance_response_summary_only(
    *,
    trade_count: int,
    total_realized_pnl: float,
    total_commission: float,
    net_pnl: float,
    win_count: int,
    loss_count: int,
    max_win: Optional[float] = None,
    max_loss: Optional[float] = None,
    avg_win: Optional[float] = None,
    avg_loss: Optional[float] = None,
    profit_factor: Optional[float] = None,
) -> Dict[str, Any]:
    win_rate = (win_count / trade_count) if trade_count else None
    return {
        "transaction": {"net_cash_flow": 0.0, "start_equity": None, "capital_base": None},
        "transactions": [],
        "summary": {
            "total_pnl": round(net_pnl, 2),
            "total_realized_pnl": round(total_realized_pnl, 2),
            "total_commission": round(total_commission, 2),
            "net_pnl": round(net_pnl, 2),
            "trade_count": trade_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round(win_rate, 4) if win_rate is not None else None,
            "profit_factor": round(profit_factor, 4)
            if profit_factor is not None and math.isfinite(profit_factor)
            else profit_factor,
            "avg_win": round(avg_win, 2) if avg_win is not None else None,
            "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
            "max_win": round(max_win, 2) if max_win is not None else None,
            "max_loss": round(max_loss, 2) if max_loss is not None else None,
            "max_drawdown": None,
            "return_pct": None,
            "total_unrealized_pnl": 0.0,
        },
        "realized_by_account": [],
        "realized_by_sec_type": [],
        "realized_by_account_and_sec_type": [],
        "realized_by_strategy_opportunity": [],
        "realized_by_strategy_instance": [],
        "calendar": [],
        "calendar_by_sec_type": [],
        "cumulative_curve": [],
        "unrealized": {"total_pnl": 0.0, "return_pct": None, "current_equity": None},
        "unrealized_by_account": [],
        "unrealized_by_sec_type": [],
        "unrealized_by_account_and_sec_type": [],
    }


def get_performance_instance_summary_only(
    conn: Any,
    strategy_instance_id: int,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Aggregate realized PnL for one strategy_instance (includes account_execution_instance_allocation splits)."""
    if conn is None:
        return _performance_response_summary_only(
            trade_count=0,
            total_realized_pnl=0.0,
            total_commission=0.0,
            net_pnl=0.0,
            win_count=0,
            loss_count=0,
        )
    try:
        sid = int(strategy_instance_id)
    except (TypeError, ValueError):
        return _performance_response_summary_only(
            trade_count=0,
            total_realized_pnl=0.0,
            total_commission=0.0,
            net_pnl=0.0,
            win_count=0,
            loss_count=0,
        )
    try:
        executions = get_executions(
            conn,
            since_ts=since_ts,
            until_ts=until_ts,
            account_id=None,
            limit=50000,
            strategy_opportunity_id=None,
            strategy_instance_id=sid,
            source_scope="performance_book",
        )
        total_rp = 0.0
        total_comm = 0.0
        trade_count = 0
        win_count = 0
        loss_count = 0
        wins_rp: List[float] = []
        losses_rp: List[float] = []
        for e in executions:
            w = weight_realized_for_strategy_instance(e, sid)
            if w <= 0:
                continue
            rp = float(e.get("realized_pnl") or 0) * w
            comm = float(e.get("commission") or 0) * w
            if not math.isfinite(rp):
                rp = 0.0
            if not math.isfinite(comm):
                comm = 0.0
            total_rp += rp
            total_comm += comm
            trade_count += 1
            if rp > 0:
                win_count += 1
                wins_rp.append(rp)
            elif rp < 0:
                loss_count += 1
                losses_rp.append(rp)
        net_pnl = total_rp - total_comm
        sum_wins = sum(wins_rp) if wins_rp else 0.0
        sum_losses_abs = abs(sum(losses_rp)) if losses_rp else 0.0
        profit_factor_n = (
            (sum_wins / sum_losses_abs) if sum_losses_abs > 0 else (None if not sum_wins else float("inf"))
        )
        avg_win_v = (sum_wins / win_count) if win_count else None
        avg_loss_v = (sum(losses_rp) / loss_count) if loss_count else None
        max_win_v = max(wins_rp) if wins_rp else None
        max_loss_v = min(losses_rp) if losses_rp else None
        return _performance_response_summary_only(
            trade_count=trade_count,
            total_realized_pnl=total_rp,
            total_commission=total_comm,
            net_pnl=net_pnl,
            win_count=win_count,
            loss_count=loss_count,
            max_win=max_win_v,
            max_loss=max_loss_v,
            avg_win=avg_win_v,
            avg_loss=avg_loss_v,
            profit_factor=profit_factor_n,
        )
    except Exception as e:
        logger.debug("get_performance_instance_summary_only failed: %s", e)
        return _performance_response_summary_only(
            trade_count=0,
            total_realized_pnl=0.0,
            total_commission=0.0,
            net_pnl=0.0,
            win_count=0,
            loss_count=0,
        )


def get_performance_stats(
    conn: Any,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    account_id: Optional[str] = None,
    granularity: str = "day",
    strategy_opportunity_id: Optional[int] = None,
    strategy_instance_id: Optional[int] = None,
    source_scope: str = "performance_book",
) -> Dict[str, Any]:
    from bifrost_core.portfolio.reader.accounts import get_accounts_from_tables

    current_equity: Optional[float] = _get_current_equity(conn)
    net_cash_flow = get_net_cash_flow(conn, since_ts=since_ts, until_ts=until_ts, account_id=account_id)
    start_equity: Optional[float] = current_equity
    capital_base: Optional[float] = (start_equity + 0.5 * net_cash_flow) if start_equity is not None else None
    if capital_base is not None and capital_base <= 0:
        capital_base = None

    scope_norm = (source_scope or "performance_book").strip().lower()
    if scope_norm not in ("performance_book", "on_the_fly"):
        scope_norm = "performance_book"

    executions = get_executions(
        conn,
        since_ts=since_ts,
        until_ts=until_ts,
        account_id=account_id,
        limit=5000,
        strategy_opportunity_id=strategy_opportunity_id,
        strategy_instance_id=strategy_instance_id,
        source_scope=scope_norm,
    )
    executions_sorted = sorted([e for e in executions if e.get("time") is not None], key=lambda e: float(e["time"]))

    def _perf_inst_weight(ex: Dict[str, Any]) -> float:
        if strategy_instance_id is None:
            return 1.0
        return weight_realized_for_strategy_instance(ex, int(strategy_instance_id))

    total_realized_pnl = 0.0
    total_commission = 0.0
    net_pnl = 0.0
    trade_count = 0
    wins: List[float] = []
    losses: List[float] = []
    cumulative_curve: List[Dict[str, Any]] = []
    running_net = 0.0
    for e in executions_sorted:
        wf = _perf_inst_weight(e)
        rp_val = float(e["realized_pnl"]) if e.get("realized_pnl") is not None and isinstance(e.get("realized_pnl"), (int, float)) else 0.0
        comm_val = float(e["commission"]) if e.get("commission") is not None and isinstance(e.get("commission"), (int, float)) else 0.0
        if not math.isfinite(rp_val):
            rp_val = 0.0
        if not math.isfinite(comm_val):
            comm_val = 0.0
        rp_val *= wf
        comm_val *= wf
        net = rp_val - comm_val
        total_realized_pnl += rp_val
        total_commission += comm_val
        net_pnl += net
        trade_count += 1
        if rp_val > 0:
            wins.append(rp_val)
        elif rp_val < 0:
            losses.append(rp_val)
        running_net += net
        t = e.get("time")
        if t is not None:
            cumulative_curve.append({"ts": float(t), "cumulative_net_pnl": round(running_net, 2)})

    win_count = len(wins)
    loss_count = len(losses)
    sum_wins = sum(wins)
    sum_losses_abs = abs(sum(losses)) if losses else 0.0
    win_rate = (win_count / trade_count) if trade_count else None
    profit_factor = (sum_wins / sum_losses_abs) if sum_losses_abs > 0 else (None if not sum_wins else float("inf"))
    avg_win = (sum_wins / win_count) if win_count else None
    avg_loss = (sum(losses) / loss_count) if loss_count else None
    max_win = max(wins) if wins else None
    max_loss = min(losses) if losses else None
    peak, max_dd = 0.0, 0.0
    for pt in cumulative_curve:
        v = pt.get("cumulative_net_pnl") or 0.0
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    max_drawdown = max_dd if max_dd > 0 else None

    by_acc: Dict[str, Dict[str, Any]] = {}
    for e in executions_sorted:
        wf = _perf_inst_weight(e)
        acc = e.get("account_id") or ""
        if acc not in by_acc:
            by_acc[acc] = {"total_pnl": 0.0, "commission": 0.0, "net_pnl": 0.0, "trade_count": 0}
        rp_val = float(e["realized_pnl"]) if e.get("realized_pnl") is not None else 0.0
        comm_val = float(e["commission"]) if e.get("commission") is not None else 0.0
        if not math.isfinite(rp_val):
            rp_val = 0.0
        if not math.isfinite(comm_val):
            comm_val = 0.0
        rp_val *= wf
        comm_val *= wf
        by_acc[acc]["total_pnl"] += rp_val
        by_acc[acc]["commission"] += comm_val
        by_acc[acc]["net_pnl"] += rp_val - comm_val
        by_acc[acc]["trade_count"] += 1
    realized_by_account = [{"account_id": acc, "total_pnl": round(v["total_pnl"], 2), "commission": round(v["commission"], 2), "net_pnl": round(v["net_pnl"], 2), "trade_count": v["trade_count"]} for acc, v in sorted(by_acc.items())]
    if capital_base and capital_base > 0:
        for row in realized_by_account:
            row["return_pct"] = round(100.0 * row["net_pnl"] / capital_base, 4)

    by_sec: Dict[str, Dict[str, Any]] = {}
    for e in executions_sorted:
        wf = _perf_inst_weight(e)
        st = (e.get("sec_type") or "UNKNOWN").strip().upper() or "UNKNOWN"
        if st not in by_sec:
            by_sec[st] = {"total_pnl": 0.0, "commission": 0.0, "net_pnl": 0.0, "trade_count": 0}
        rp_val = float(e["realized_pnl"]) if e.get("realized_pnl") is not None else 0.0
        comm_val = float(e["commission"]) if e.get("commission") is not None else 0.0
        if not math.isfinite(rp_val):
            rp_val = 0.0
        if not math.isfinite(comm_val):
            comm_val = 0.0
        rp_val *= wf
        comm_val *= wf
        by_sec[st]["total_pnl"] += rp_val
        by_sec[st]["commission"] += comm_val
        by_sec[st]["net_pnl"] += rp_val - comm_val
        by_sec[st]["trade_count"] += 1
    realized_by_sec_type = [{"sec_type": st, "total_pnl": round(v["total_pnl"], 2), "commission": round(v["commission"], 2), "net_pnl": round(v["net_pnl"], 2), "trade_count": v["trade_count"]} for st, v in sorted(by_sec.items())]
    if capital_base and capital_base > 0:
        for row in realized_by_sec_type:
            row["return_pct"] = round(100.0 * row["net_pnl"] / capital_base, 4)

    by_acc_sec: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for e in executions_sorted:
        wf = _perf_inst_weight(e)
        acc, st = e.get("account_id") or "", (e.get("sec_type") or "UNKNOWN").strip().upper() or "UNKNOWN"
        key = (acc, st)
        if key not in by_acc_sec:
            by_acc_sec[key] = {"total_pnl": 0.0, "commission": 0.0, "net_pnl": 0.0, "trade_count": 0}
        rp_val = float(e["realized_pnl"]) if e.get("realized_pnl") is not None else 0.0
        comm_val = float(e["commission"]) if e.get("commission") is not None else 0.0
        if not math.isfinite(rp_val):
            rp_val = 0.0
        if not math.isfinite(comm_val):
            comm_val = 0.0
        rp_val *= wf
        comm_val *= wf
        by_acc_sec[key]["total_pnl"] += rp_val
        by_acc_sec[key]["commission"] += comm_val
        by_acc_sec[key]["net_pnl"] += rp_val - comm_val
        by_acc_sec[key]["trade_count"] += 1
    realized_by_account_and_sec_type = [{"account_id": k[0], "sec_type": k[1], "total_pnl": round(v["total_pnl"], 2), "commission": round(v["commission"], 2), "net_pnl": round(v["net_pnl"], 2), "trade_count": v["trade_count"]} for k, v in sorted(by_acc_sec.items())]
    if capital_base and capital_base > 0:
        for row in realized_by_account_and_sec_type:
            row["return_pct"] = round(100.0 * row["net_pnl"] / capital_base, 4)

    by_opp: Dict[int, Dict[str, Any]] = {}
    by_inst: Dict[int, Dict[str, Any]] = {}
    for e in executions_sorted:
        _add_realized_splits_to_opp_and_inst(
            e,
            by_opp,
            by_inst,
            only_strategy_instance_id=strategy_instance_id,
        )
    realized_by_strategy_opportunity = [{"strategy_opportunity_id": k, "total_pnl": round(v["total_pnl"], 2), "commission": round(v["commission"], 2), "net_pnl": round(v["net_pnl"], 2), "trade_count": v["trade_count"]} for k, v in sorted(by_opp.items())]
    realized_by_strategy_instance = [{"strategy_instance_id": k, "total_pnl": round(v["total_pnl"], 2), "commission": round(v["commission"], 2), "net_pnl": round(v["net_pnl"], 2), "trade_count": v["trade_count"]} for k, v in sorted(by_inst.items())]
    if capital_base and capital_base > 0:
        for row in realized_by_strategy_opportunity:
            row["return_pct"] = round(100.0 * row["net_pnl"] / capital_base, 4)
        for row in realized_by_strategy_instance:
            row["return_pct"] = round(100.0 * row["net_pnl"] / capital_base, 4)

    def _period_key(ts: float, gran: str) -> Tuple[float, str]:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        d = dt.date()
        if gran == "month":
            start = date(d.year, d.month, 1)
            label = start.strftime("%Y-%m")
            start_ts = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()
        elif gran == "week":
            start = d - timedelta(days=d.weekday())
            label = start.strftime("%Y-%m-%d")
            start_ts = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()
        else:
            start = d
            label = start.strftime("%Y-%m-%d")
            start_ts = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()
        return (start_ts, label)

    cal_map: Dict[Tuple[float, str], Dict[str, Any]] = {}
    for e in executions_sorted:
        wf = _perf_inst_weight(e)
        t = e.get("time")
        if t is None:
            continue
        ts = float(t)
        start_ts, label = _period_key(ts, granularity)
        key = (start_ts, label)
        if key not in cal_map:
            cal_map[key] = {"period_start_ts": start_ts, "period_label": label, "pnl": 0.0, "commission": 0.0, "net_pnl": 0.0, "trade_count": 0, "win_count": 0, "loss_count": 0}
        rp_val = float(e["realized_pnl"]) if e.get("realized_pnl") is not None else 0.0
        comm_val = float(e["commission"]) if e.get("commission") is not None else 0.0
        if not math.isfinite(rp_val):
            rp_val = 0.0
        if not math.isfinite(comm_val):
            comm_val = 0.0
        rp_val *= wf
        comm_val *= wf
        cal_map[key]["pnl"] += rp_val
        cal_map[key]["commission"] += comm_val
        cal_map[key]["net_pnl"] += rp_val - comm_val
        cal_map[key]["trade_count"] += 1
        if rp_val > 0:
            cal_map[key]["win_count"] += 1
        elif rp_val < 0:
            cal_map[key]["loss_count"] += 1
    calendar = []
    for _, v in sorted(cal_map.items(), key=lambda x: x[0][0]):
        wc, lc = v.get("win_count", 0), v.get("loss_count", 0)
        v["win_rate"] = (wc / (wc + lc)) if (wc + lc) > 0 else None
        calendar.append(v)
    if capital_base and capital_base > 0:
        for row in calendar:
            row["return_pct"] = round(100.0 * row["net_pnl"] / capital_base, 4)

    opt_calendar = _compute_opt_realized_calendar(executions_sorted, granularity)
    cal_map_by_sec: Dict[Tuple[float, str, str], Dict[str, Any]] = {}
    for e in executions_sorted:
        wf = _perf_inst_weight(e)
        st = (e.get("sec_type") or "UNKNOWN").strip().upper() or "UNKNOWN"
        if st == "OPT":
            continue
        t = e.get("time")
        if t is None:
            continue
        ts = float(t)
        start_ts, label = _period_key(ts, granularity)
        key = (start_ts, label, st)
        if key not in cal_map_by_sec:
            cal_map_by_sec[key] = {"period_start_ts": start_ts, "period_label": label, "sec_type": st, "pnl": 0.0, "commission": 0.0, "net_pnl": 0.0, "trade_count": 0, "win_count": 0, "loss_count": 0}
        rp_val = float(e["realized_pnl"]) if e.get("realized_pnl") is not None else 0.0
        comm_val = float(e["commission"]) if e.get("commission") is not None else 0.0
        if not math.isfinite(rp_val):
            rp_val = 0.0
        if not math.isfinite(comm_val):
            comm_val = 0.0
        rp_val *= wf
        comm_val *= wf
        cal_map_by_sec[key]["pnl"] += rp_val
        cal_map_by_sec[key]["commission"] += comm_val
        cal_map_by_sec[key]["net_pnl"] += rp_val - comm_val
        cal_map_by_sec[key]["trade_count"] += 1
        if rp_val > 0:
            cal_map_by_sec[key]["win_count"] += 1
        elif rp_val < 0:
            cal_map_by_sec[key]["loss_count"] += 1
    calendar_by_sec_type = list(opt_calendar)
    for k, v in sorted(cal_map_by_sec.items(), key=lambda x: (x[0][0], x[0][2])):
        wc, lc = v.get("win_count", 0), v.get("loss_count", 0)
        v["win_rate"] = (wc / (wc + lc)) if (wc + lc) > 0 else None
        calendar_by_sec_type.append(v)
    calendar_by_sec_type.sort(key=lambda x: (x["period_start_ts"], x["sec_type"]))
    if capital_base and capital_base > 0:
        for row in calendar_by_sec_type:
            row["return_pct"] = round(100.0 * row["net_pnl"] / capital_base, 4)

    accounts_list = get_accounts_from_tables(conn) or []
    if scope_norm == "on_the_fly":
        total_unrealized = 0.0
        unrealized = {"total_pnl": 0.0, "return_pct": None, "current_equity": None}
        unrealized_by_account: List[Dict[str, Any]] = []
        unrealized_by_sec_type: List[Dict[str, Any]] = []
        unrealized_by_account_and_sec_type: List[Dict[str, Any]] = []
    else:
        total_unrealized = 0.0
        unrel_by_acc: Dict[str, float] = {}
        unrel_by_sec: Dict[str, float] = {}
        for acc_obj in accounts_list:
            acc_id = acc_obj.get("account_id") or ""
            for pos in acc_obj.get("positions") or []:
                up = pos.get("unrealized_pnl")
                if up is not None and isinstance(up, (int, float)) and math.isfinite(up):
                    total_unrealized += up
                    unrel_by_acc[acc_id] = unrel_by_acc.get(acc_id, 0.0) + up
                    st = (pos.get("secType") or pos.get("sec_type") or "UNKNOWN").strip().upper() or "UNKNOWN"
                    unrel_by_sec[st] = unrel_by_sec.get(st, 0.0) + up
        unrealized = {
            "total_pnl": round(total_unrealized, 2),
            "return_pct": round(100.0 * total_unrealized / current_equity, 4) if current_equity and current_equity > 0 else None,
            "current_equity": round(current_equity, 2) if current_equity is not None else None,
        }
        unrealized_by_account = [{"account_id": acc, "total_pnl": round(v, 2)} for acc, v in sorted(unrel_by_acc.items())]
        unrealized_by_sec_type = [{"sec_type": st, "total_pnl": round(v, 2)} for st, v in sorted(unrel_by_sec.items())]
        unrel_by_acc_sec: Dict[Tuple[str, str], float] = {}
        for acc_obj in accounts_list:
            acc_id = acc_obj.get("account_id") or ""
            for pos in acc_obj.get("positions") or []:
                up = pos.get("unrealized_pnl")
                if up is not None and isinstance(up, (int, float)) and math.isfinite(up):
                    st = (pos.get("secType") or pos.get("sec_type") or "UNKNOWN").strip().upper() or "UNKNOWN"
                    key = (acc_id, st)
                    unrel_by_acc_sec[key] = unrel_by_acc_sec.get(key, 0.0) + up
        unrealized_by_account_and_sec_type = [
            {"account_id": k[0], "sec_type": k[1], "total_pnl": round(v, 2)} for k, v in sorted(unrel_by_acc_sec.items())
        ]

    total_pnl = net_pnl + total_unrealized
    return_pct = round(100.0 * total_pnl / capital_base, 4) if capital_base and capital_base > 0 else None

    return {
        "transaction": {"net_cash_flow": net_cash_flow, "start_equity": round(start_equity, 2) if start_equity is not None else None, "capital_base": round(capital_base, 2) if capital_base is not None else None},
        "transactions": get_transactions(conn, since_ts=since_ts, until_ts=until_ts, account_id=account_id, limit=200),
        "summary": {
            "total_pnl": round(total_pnl, 2),
            "total_realized_pnl": round(total_realized_pnl, 2),
            "total_commission": round(total_commission, 2),
            "net_pnl": round(net_pnl, 2),
            "trade_count": trade_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": round(win_rate, 4) if win_rate is not None else None,
            "profit_factor": round(profit_factor, 4) if profit_factor is not None and math.isfinite(profit_factor) else profit_factor,
            "avg_win": round(avg_win, 2) if avg_win is not None else None,
            "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
            "max_win": round(max_win, 2) if max_win is not None else None,
            "max_loss": round(max_loss, 2) if max_loss is not None else None,
            "max_drawdown": round(max_drawdown, 2) if max_drawdown is not None else None,
            "return_pct": return_pct,
            "total_unrealized_pnl": round(total_unrealized, 2),
        },
        "realized_by_account": realized_by_account,
        "realized_by_sec_type": realized_by_sec_type,
        "realized_by_account_and_sec_type": realized_by_account_and_sec_type,
        "realized_by_strategy_opportunity": realized_by_strategy_opportunity,
        "realized_by_strategy_instance": realized_by_strategy_instance,
        "calendar": calendar,
        "calendar_by_sec_type": calendar_by_sec_type,
        "cumulative_curve": cumulative_curve,
        "unrealized": unrealized,
        "unrealized_by_account": unrealized_by_account,
        "unrealized_by_sec_type": unrealized_by_sec_type,
        "unrealized_by_account_and_sec_type": unrealized_by_account_and_sec_type,
    }


# ---------------------------------------------------------------------------
# Position × Instance attribution (net-estimated, real-time read model)
# ---------------------------------------------------------------------------

# Join account_positions row `p` to execution row `e` (final view or executions_raw_tws).
_POS_EXEC_JOIN_PE = """(
  (
    upper(trim(COALESCE(split_part(p.contract_key, '|', 2), p.sec_type, ''))) = 'OPT'
    AND upper(trim(COALESCE(split_part(e.contract_key, '|', 2), e.sec_type, ''))) = 'OPT'
    AND trim(COALESCE(split_part(p.contract_key, '|', 3), p.expiry, '')) = trim(COALESCE(split_part(e.contract_key, '|', 3), e.expiry, ''))
    AND upper(trim(COALESCE(split_part(p.contract_key, '|', 5), p.option_right, ''))) = upper(trim(COALESCE(split_part(e.contract_key, '|', 5), e.option_right, '')))
    AND NULLIF(trim(COALESCE(split_part(p.contract_key, '|', 4), '')), '') IS NOT NULL
    AND NULLIF(trim(COALESCE(split_part(e.contract_key, '|', 4), '')), '') IS NOT NULL
    AND abs((split_part(p.contract_key, '|', 4))::double precision - (split_part(e.contract_key, '|', 4))::double precision) < 1e-9
  )
  OR (
    upper(trim(COALESCE(p.sec_type, ''))) <> 'OPT'
    AND p.contract_key = e.contract_key
  )
)"""

# Per-row signed qty for account_executions_final (flex/journal; same convention as net aggregation elsewhere).
_SIGNED_QTY_FINAL_ROW_E = (
    "CASE WHEN lower(trim(COALESCE(e.source, ''))) = 'tws_client' THEN e.quantity "
    "WHEN upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S') THEN -abs(e.quantity) "
    "ELSE abs(e.quantity) END"
)

# executions_raw_tws: quantity stored unsigned; sign from side only.
_SIGNED_QTY_TWS_RAW_ROW_E = (
    "CASE WHEN upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S') THEN -abs(e.quantity) "
    "ELSE abs(e.quantity) END"
)


def get_position_instance_attribution(
    conn: Any,
    account_id: Optional[str] = None,
    sec_type_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Compute Position × Instance attribution.

    Execution source rule (per physical position / contract):
    if any row in account_executions_final matches the position, only those
    executions contribute; otherwise executions_raw_tws only.

    open_qty_est per instance = SUM(signed execution quantity) for that instance
    (not a proportional split of broker position). PnL estimate scales with
    open_qty_est. All instances with non-zero contribution appear under the
    instance (no same-sign filter that dropped rows).
    """
    if conn is None:
        return []
    try:
        pos_conds: List[str] = ["ap.position != 0"]
        pos_vals: List[Any] = []
        if account_id is not None and account_id.strip():
            pos_conds.append("ap.account_id = %s")
            pos_vals.append(account_id.strip())
        if sec_type_filter:
            pos_conds.append("upper(trim(COALESCE(ap.sec_type, ''))) = %s")
            pos_vals.append(sec_type_filter.strip().upper())
        pos_where = " AND ".join(pos_conds)

        sql = f"""
        WITH pos AS (
            SELECT ap.account_id, ap.contract_key, ap.symbol, ap.sec_type,
                   ap.position, ap.avg_cost, ap.expiry, ap.strike, ap.option_right,
                   cql.mid AS price_mid, cql.last AS price_last
            FROM {POSITIONS} ap
            LEFT JOIN {CONTRACT_QUOTE_LIVE} cql ON ap.contract_key = cql.contract_key
            WHERE {pos_where}
        ),
        pos_has_final AS (
            SELECT DISTINCT p.account_id, p.contract_key
            FROM pos p
            INNER JOIN {_EXEC_FINAL_TABLE} e ON p.account_id = e.account_id AND {_POS_EXEC_JOIN_PE}
        ),
        exec_labeled AS (
            SELECT p.account_id, p.contract_key AS pos_contract_key,
                   e.strategy_instance_id,
                   COALESCE(e.strategy_opportunity_id, si2.strategy_opportunity_id) AS strategy_opportunity_id,
                   {_SIGNED_QTY_FINAL_ROW_E} AS signed_qty
            FROM pos p
            INNER JOIN {_EXEC_FINAL_TABLE} e ON p.account_id = e.account_id AND {_POS_EXEC_JOIN_PE}
            INNER JOIN pos_has_final hf ON hf.account_id = p.account_id AND hf.contract_key = p.contract_key
            LEFT JOIN strategy_instance si2 ON e.strategy_instance_id = si2.strategy_instance_id
            WHERE NOT EXISTS (
                SELECT 1 FROM {_EXEC_INST_ALLOC_TABLE} ax
                WHERE ax.account_executions_id = e.account_executions_id
                  AND ax.account_id IS NOT DISTINCT FROM e.account_id
            )
            UNION ALL
            SELECT p.account_id, p.contract_key AS pos_contract_key,
                   a.strategy_instance_id,
                   COALESCE(si_a.strategy_opportunity_id, e.strategy_opportunity_id) AS strategy_opportunity_id,
                   a.allocated_quantity AS signed_qty
            FROM pos p
            INNER JOIN {_EXEC_FINAL_TABLE} e ON p.account_id = e.account_id AND {_POS_EXEC_JOIN_PE}
            INNER JOIN pos_has_final hf ON hf.account_id = p.account_id AND hf.contract_key = p.contract_key
            INNER JOIN {_EXEC_INST_ALLOC_TABLE} a ON a.account_executions_id = e.account_executions_id
              AND a.account_id IS NOT DISTINCT FROM e.account_id
            LEFT JOIN strategy_instance si_a ON a.strategy_instance_id = si_a.strategy_instance_id
            UNION ALL
            SELECT p.account_id, p.contract_key AS pos_contract_key,
                   e.strategy_instance_id,
                   COALESCE(e.strategy_opportunity_id, si2.strategy_opportunity_id) AS strategy_opportunity_id,
                   {_SIGNED_QTY_TWS_RAW_ROW_E} AS signed_qty
            FROM pos p
            INNER JOIN {EXECUTIONS_RAW_TWS} e ON p.account_id = e.account_id AND {_POS_EXEC_JOIN_PE}
            LEFT JOIN strategy_instance si2 ON e.strategy_instance_id = si2.strategy_instance_id
            WHERE NOT EXISTS (
                SELECT 1 FROM pos_has_final hf
                WHERE hf.account_id = p.account_id AND hf.contract_key = p.contract_key
            )
              AND NOT EXISTS (
                SELECT 1 FROM {_EXEC_INST_ALLOC_TABLE} ax
                WHERE ax.account_executions_id = -(e.executions_raw_tws_id)
                  AND ax.account_id IS NOT DISTINCT FROM e.account_id
            )
            UNION ALL
            SELECT p.account_id, p.contract_key AS pos_contract_key,
                   a.strategy_instance_id,
                   COALESCE(si_a.strategy_opportunity_id, e.strategy_opportunity_id) AS strategy_opportunity_id,
                   a.allocated_quantity AS signed_qty
            FROM pos p
            INNER JOIN {EXECUTIONS_RAW_TWS} e ON p.account_id = e.account_id AND {_POS_EXEC_JOIN_PE}
            INNER JOIN {_EXEC_INST_ALLOC_TABLE} a ON a.account_executions_id = -(e.executions_raw_tws_id)
              AND a.account_id IS NOT DISTINCT FROM e.account_id
            LEFT JOIN strategy_instance si_a ON a.strategy_instance_id = si_a.strategy_instance_id
            WHERE NOT EXISTS (
                SELECT 1 FROM pos_has_final hf
                WHERE hf.account_id = p.account_id AND hf.contract_key = p.contract_key
            )
        ),
        exec_grouped AS (
            SELECT account_id, pos_contract_key, strategy_instance_id,
                   MAX(strategy_opportunity_id) AS strategy_opportunity_id,
                   SUM(signed_qty) AS net_qty_contribution,
                   COUNT(*) AS exec_count
            FROM exec_labeled
            GROUP BY account_id, pos_contract_key, strategy_instance_id
        )
        SELECT
            p.account_id, p.contract_key, p.symbol, p.sec_type,
            p.position AS position_qty, p.avg_cost, p.expiry, p.strike, p.option_right,
            p.price_mid, p.price_last,
            eg.strategy_instance_id,
            eg.strategy_opportunity_id,
            si.label AS strategy_instance_label,
            so.name AS strategy_opportunity_name,
            EXTRACT(EPOCH FROM si.opened_at)::bigint AS strategy_instance_opened_at_epoch,
            ss.name AS structure_type,
            so.scope_type,
            ss.strategy_structure_id,
            eg.net_qty_contribution,
            eg.exec_count
        FROM pos p
        LEFT JOIN exec_grouped eg ON p.account_id = eg.account_id AND p.contract_key = eg.pos_contract_key
        LEFT JOIN strategy_instance si ON eg.strategy_instance_id = si.strategy_instance_id
        LEFT JOIN strategy_opportunity so ON eg.strategy_opportunity_id = so.strategy_opportunity_id
        LEFT JOIN strategy_structure ss ON so.strategy_structure_id = ss.strategy_structure_id
        ORDER BY p.account_id, p.contract_key, eg.strategy_instance_id NULLS LAST
        """

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, pos_vals)
            rows = cur.fetchall()

        return _build_attribution_rows(rows)
    except Exception as e:
        logger.warning("get_position_instance_attribution failed: %s", e)
        return []


def _build_attribution_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Post-process SQL rows into attribution results with ratios and PnL estimates."""
    from collections import defaultdict

    pos_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    pos_meta: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        key = f"{(r.get('account_id') or '').strip()}\x00{(r.get('contract_key') or '').strip()}"
        pos_groups[key].append(r)
        if key not in pos_meta:
            pos_meta[key] = r

    result: List[Dict[str, Any]] = []
    for key, group in pos_groups.items():
        meta = pos_meta[key]
        pos_qty = float(meta.get("position_qty") or 0)
        avg_cost = meta.get("avg_cost")
        sec_type = (meta.get("sec_type") or "").strip().upper()

        price_for_pnl: Optional[float] = None
        for candidate in (meta.get("price_last"), meta.get("price_mid")):
            if candidate is not None:
                try:
                    v = float(candidate)
                    if math.isfinite(v) and v > 0:
                        price_for_pnl = v
                        break
                except (TypeError, ValueError):
                    pass

        total_unrealized: Optional[float] = None
        if price_for_pnl is not None and avg_cost is not None and pos_qty != 0:
            try:
                c = float(avg_cost)
                if math.isfinite(c):
                    mult = 100 if sec_type == "OPT" else 1
                    total_unrealized = round((price_for_pnl - c) * pos_qty * mult, 2)
            except (TypeError, ValueError):
                pass

        contrib_rows = [
            r
            for r in group
            if r.get("exec_count") is not None and int(r.get("exec_count") or 0) > 0
        ]
        has_any_exec = len(contrib_rows) > 0

        instance_ids = set()
        has_unassigned_execs = False
        for r in contrib_rows:
            sid = r.get("strategy_instance_id")
            instance_ids.add(sid)
            if sid is None:
                has_unassigned_execs = True

        distinct_instances = {sid for sid in instance_ids if sid is not None}
        is_mixed = len(distinct_instances) > 1 or (len(distinct_instances) >= 1 and has_unassigned_execs)

        if not has_any_exec:
            result.append(_make_attribution_row(
                meta, pos_qty, total_unrealized,
                strategy_instance_id=None,
                strategy_instance_label=None,
                strategy_opportunity_id=None,
                strategy_opportunity_name=None,
                strategy_instance_opened_at_epoch=None,
                structure_type=None,
                scope_type=None,
                strategy_structure_id=None,
                open_qty_est=pos_qty,
                attribution_ratio=1.0,
                unrealized_pnl_est=total_unrealized,
                source_exec_count=0,
                is_mixed=False,
                has_unassigned=True,
                method="net_estimated",
            ))
            continue

        sum_abs_nq = sum(abs(float(r.get("net_qty_contribution") or 0)) for r in contrib_rows)

        def _pnl_for_open_qty(open_qty: float) -> Optional[float]:
            if price_for_pnl is None or avg_cost is None:
                return None
            try:
                c = float(avg_cost)
                if not math.isfinite(c):
                    return None
                mult = 100 if sec_type == "OPT" else 1
                return round((price_for_pnl - c) * open_qty * mult, 2)
            except (TypeError, ValueError):
                return None

        for r in contrib_rows:
            nq = float(r.get("net_qty_contribution") or 0)
            open_qty = round(nq, 6)
            ratio = round(abs(nq) / sum_abs_nq, 6) if sum_abs_nq > 0 else 0.0
            pnl_est = _pnl_for_open_qty(open_qty)
            result.append(_make_attribution_row(
                meta, pos_qty, total_unrealized,
                strategy_instance_id=r.get("strategy_instance_id"),
                strategy_instance_label=r.get("strategy_instance_label"),
                strategy_opportunity_id=r.get("strategy_opportunity_id"),
                strategy_opportunity_name=r.get("strategy_opportunity_name"),
                strategy_instance_opened_at_epoch=r.get("strategy_instance_opened_at_epoch"),
                structure_type=r.get("structure_type"),
                scope_type=r.get("scope_type"),
                strategy_structure_id=r.get("strategy_structure_id"),
                open_qty_est=open_qty,
                attribution_ratio=ratio,
                unrealized_pnl_est=pnl_est,
                source_exec_count=int(r.get("exec_count") or 0),
                is_mixed=is_mixed,
                has_unassigned=has_unassigned_execs,
                method="net_estimated",
            ))

    return result


def _make_attribution_row(
    meta: Dict[str, Any],
    position_qty: float,
    total_unrealized: Optional[float],
    *,
    strategy_instance_id: Optional[int],
    strategy_instance_label: Optional[str],
    strategy_opportunity_id: Optional[int],
    strategy_opportunity_name: Optional[str],
    strategy_instance_opened_at_epoch: Optional[int],
    structure_type: Optional[str],
    scope_type: Optional[str],
    strategy_structure_id: Optional[int],
    open_qty_est: float,
    attribution_ratio: float,
    unrealized_pnl_est: Optional[float],
    source_exec_count: int,
    is_mixed: bool,
    has_unassigned: bool,
    method: str,
) -> Dict[str, Any]:
    return {
        "account_id": (meta.get("account_id") or "").strip(),
        "contract_key": (meta.get("contract_key") or "").strip(),
        "symbol": (meta.get("symbol") or "").strip(),
        "sec_type": (meta.get("sec_type") or "").strip(),
        "expiry": (meta.get("expiry") or "").strip(),
        "strike": meta.get("strike"),
        "option_right": (meta.get("option_right") or "").strip(),
        "position_qty": position_qty,
        "avg_cost": meta.get("avg_cost"),
        "price_mid": meta.get("price_mid"),
        "price_last": meta.get("price_last"),
        "strategy_instance_id": strategy_instance_id,
        "strategy_instance_label": (strategy_instance_label or "").strip() if strategy_instance_label else None,
        "strategy_opportunity_id": strategy_opportunity_id,
        "strategy_opportunity_name": (strategy_opportunity_name or "").strip() if strategy_opportunity_name else None,
        "strategy_instance_opened_at_epoch": strategy_instance_opened_at_epoch,
        "structure_type": (structure_type or "").strip() if structure_type else None,
        "scope_type": (scope_type or "").strip() if scope_type else None,
        "strategy_structure_id": strategy_structure_id,
        "open_qty_est": open_qty_est,
        "attribution_ratio": attribution_ratio,
        "unrealized_pnl_est": unrealized_pnl_est,
        "source_exec_count": source_exec_count,
        "is_mixed": is_mixed,
        "has_unassigned": has_unassigned,
        "method": method,
    }
