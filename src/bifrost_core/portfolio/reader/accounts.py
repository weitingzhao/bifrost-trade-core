"""Accounts: snapshot read/write and execution/transaction write.
Execution/transaction read and preference_position_categories live in executions and position_categories modules. All logic inlined from legacy; no dependency on _legacy."""

import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.accounts_sync import sync_accounts_snapshot_to_tables
from bifrost_core.persistence.postgres.commissions import stored_commission, upsert_commission
from bifrost_core.portfolio.units import option_cost_per_share, position_value
from bifrost_core.persistence.postgres.brokerage_tables import (
    ACCOUNT,
    EXECUTIONS,
    EXECUTIONS_RAW_FLEX,
    EXECUTIONS_RAW_JOURNAL,
    EXECUTIONS_RAW_TWS,
    GOLDEN_COMMISSIONS,
    GOLDEN_EXECUTIONS_RAW_FLEX,
    GOLDEN_EXECUTIONS_RAW_JOURNAL,
    GOLDEN_EXECUTIONS_RAW_TWS,
    GOLDEN_TRANSACTIONS,
    OPTION_STOCK_LINK,
    POSITIONS,
    TRADE_EXECUTION,
    TRADE_FILL_SPLITS,
)

from bifrost_core.monitor.reader import market as market_module
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteConflict, WriteFailed, WriteInvalid, WriteNotFound
from bifrost_core.portfolio.contract_key import TWS_SOURCES, tws_execution_opt_key
from bifrost_core.portfolio.signed_qty import signed_qty
from bifrost_core.portfolio.reader.accounts_helpers import (
    _exec_time_to_dt,
    _has_meaningful_commission,
    resolve_daily_prev_close_from_fallback,
)

logger = logging.getLogger(__name__)

# Logical account_executions_id in unified views → physical raw table + PK (see persistence postgres ddl account_executions / account_executions_final).
_JOURNAL_ID_OFFSET = 1000000000
_EXEC_READ_TABLE = EXECUTIONS


def _normalized_signed_qty_from_raw(source: Any, side: Any, quantity: Any) -> float:
    """The execution quantity split allocations must sum to: portfolio.signed_qty (TD-30),
    0.0 when the row has none. A sell's splits sum to a negative number, which is what the
    execution form sends; TWS sells (stored positive) used to expect a positive sum."""
    q = signed_qty(source, side, quantity)
    return 0.0 if q is None else q


# --- TD-09: attribution lives in this env's trade_execution (named so since naming R3) ---
#
# Keyed by the fill (account_id, exec_id): a TWS row and its Flex twin share the key, so
# one write attributes both. A row with NULL split_quantity is the whole fill; split
# rows carry their share. Golden Source's raw strategy_* columns are no longer written.

OPPORTUNITY_ONLY = (
    "A fill is attributed to a trade (trade_id); its opportunity is the trade's. "
    "Send trade_id."
)


def _fill_key(raw_cur: Any, raw_tbl: str, pk_col: str, pk_val: int, *, lock: bool = False) -> Optional[Tuple[str, str, float]]:
    """(account_id, exec_id, signed quantity) of one raw row; None when there is no such row.
    exec_id is '' when the row has none (such a row cannot be attributed)."""
    raw_cur.execute(
        f"SELECT account_id, quantity, side, source, exec_id FROM {raw_tbl} WHERE {pk_col} = %s"
        + (" FOR UPDATE" if lock else ""),
        (pk_val,),
    )
    row = raw_cur.fetchone()
    if not row:
        return None
    return (
        (row[0] or "").strip(),
        (row[4] or "").strip(),
        _normalized_signed_qty_from_raw(row[3], row[2], row[1]),
    )


def _instance_problem(cur: Any, instance_id: int, account_id: str, opportunity_id: Optional[int] = None) -> Optional[str]:
    """Why ``instance_id`` cannot take a fill of ``account_id`` (None when it can). When an
    opportunity is sent with it, it must be the instance's own."""
    cur.execute(
        "SELECT account_id, strategy_opportunity_id FROM trade WHERE trade_id = %s",
        (instance_id,),
    )
    inst = cur.fetchone()
    if inst is None:
        return f"No trade {instance_id}."
    if (inst[0] or "").strip() != account_id:
        return f"Trade {instance_id} belongs to account {inst[0]}, and the fill to {account_id}."
    if opportunity_id is not None and len(inst) > 1 and inst[1] is not None and int(inst[1]) != int(opportunity_id):
        return f"Trade {instance_id} is under opportunity {inst[1]}, not {opportunity_id}."
    return None


def _split_count(cur: Any, account_id: str, exec_id: str) -> int:
    cur.execute(
        f"SELECT count(*) FROM {TRADE_EXECUTION} "
        "WHERE account_id = %s AND exec_id = %s AND split_quantity IS NOT NULL",
        (account_id, exec_id),
    )
    return int((cur.fetchone() or [0])[0] or 0)


def _drop_attribution_if_last(raw_cur: Any, env_cur: Any, account_id: str, exec_id: str) -> int:
    """After a raw row is deleted: remove the fill's attribution unless another raw row
    (its TWS / Flex twin) still carries (account_id, exec_id). Returns the split rows removed."""
    if not exec_id:
        return 0
    for table in (GOLDEN_EXECUTIONS_RAW_TWS, GOLDEN_EXECUTIONS_RAW_FLEX, GOLDEN_EXECUTIONS_RAW_JOURNAL):
        raw_cur.execute(f"SELECT 1 FROM {table} WHERE account_id = %s AND exec_id = %s LIMIT 1", (account_id, exec_id))
        if raw_cur.fetchone():
            return 0
    env_cur.execute(
        f"DELETE FROM {TRADE_EXECUTION} WHERE account_id = %s AND exec_id = %s "
        "RETURNING split_quantity IS NOT NULL",
        (account_id, exec_id),
    )
    return sum(1 for r in (env_cur.fetchall() or []) if r and r[0])


def _set_whole_attribution(cur: Any, account_id: str, exec_id: str, instance_id: Optional[int]) -> None:
    """Attribute the whole fill to ``instance_id``; None clears it. Splits are not touched."""
    if instance_id is None:
        cur.execute(
            f"DELETE FROM {TRADE_EXECUTION} "
            "WHERE account_id = %s AND exec_id = %s AND split_quantity IS NULL",
            (account_id, exec_id),
        )
        return
    cur.execute(
        f"""
        INSERT INTO {TRADE_EXECUTION} (account_id, exec_id, trade_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (account_id, exec_id) WHERE split_quantity IS NULL
        DO UPDATE SET trade_id = EXCLUDED.trade_id, updated_at = now()
        """,
        (account_id, exec_id, int(instance_id)),
    )


def _direct_instance(fields: Dict[str, Any]) -> Tuple[bool, Optional[int], Optional[int]]:
    """From a write body's two ids: (touches the whole-fill attribution, instance, opportunity).

    The opportunity is the instance's and is not stored; sent alone (non-null) it is
    refused by the callers (OPPORTUNITY_ONLY). ``strategy_opportunity_id: null`` alone
    changes nothing."""
    touches = "trade_id" in fields
    inst = fields.get("trade_id")
    opp = fields.get("strategy_opportunity_id")
    return touches, (int(inst) if inst is not None else None), (int(opp) if opp is not None else None)


def _apply_fill_splits_on_cursor(
    cur: Any,
    account_executions_id: int,
    raw_tbl: str,
    pk_col: str,
    pk_val: int,
    body_allocations: List[Dict[str, Any]],
    *,
    raw_cur: Any = None,
) -> bool:
    """Replace the fill's split rows in this env's trade_execution.

    ``[]`` removes the splits; a non-empty list replaces them and clears the whole-fill
    row (a fill is attributed one way or the other). Each split names a distinct
    instance of the fill's account with a non-zero quantity, and they add up to the
    fill's signed quantity. ``cur`` is the per-env cursor; ``raw_cur`` the Golden
    Source one for the raw row (defaults to ``cur``). ``account_executions_id`` is
    kept for the callers; the key is the raw row's (account_id, exec_id).
    """
    rcur = raw_cur if raw_cur is not None else cur
    key = _fill_key(rcur, raw_tbl, pk_col, pk_val)
    if key is None:
        return False
    acc_id, exec_id, expected = key
    if not exec_id:
        return False
    inserts: List[Tuple[int, float]] = []
    total = 0.0
    for item in body_allocations:
        if not isinstance(item, dict):
            return False
        si_raw = item.get("trade_id")
        aq_raw = item.get("quantity")
        if si_raw is None or aq_raw is None:
            return False
        try:
            si_id = int(si_raw)
            aq = float(aq_raw)
        except (TypeError, ValueError):
            return False
        if aq == 0 or not math.isfinite(aq):
            return False
        if _instance_problem(cur, si_id, acc_id) is not None:
            return False
        inserts.append((si_id, aq))
        total += aq
    if len({x[0] for x in inserts}) != len(inserts):
        return False
    if inserts and abs(total - expected) > 1e-5 * max(1.0, abs(expected)):
        return False
    cur.execute(
        f"DELETE FROM {TRADE_EXECUTION} "
        "WHERE account_id = %s AND exec_id = %s AND split_quantity IS NOT NULL",
        (acc_id, exec_id),
    )
    if not inserts:
        return True
    _set_whole_attribution(cur, acc_id, exec_id, None)
    for si_id, aq in inserts:
        cur.execute(
            f"""
            INSERT INTO {TRADE_EXECUTION} (account_id, exec_id, trade_id, split_quantity)
            VALUES (%s, %s, %s, %s)
            """,
            (acc_id, exec_id, si_id, aq),
        )
    return True


def replace_execution_fill_splits(
    status_config: dict,
    account_executions_id: int,
    body_allocations: Any,
) -> bool:
    """Replace or clear the fill's split rows (trade_execution). body_allocations: None=skip, []=delete all, list=replace."""
    if body_allocations is None:
        return True
    if not isinstance(body_allocations, list):
        return False
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    raw_tbl, pk_col, pk_val = _raw_table_pk_for_account_executions_id(account_executions_id)
    try:
        conn = ws.open_conn(status_config)
        golden = ws.open_conn(status_config, golden=True)
        try:
            with conn.cursor() as cur, golden.cursor() as gcur:
                if not _apply_fill_splits_on_cursor(
                    cur, account_executions_id, raw_tbl, pk_col, pk_val, body_allocations, raw_cur=gcur
                ):
                    conn.rollback()
                    golden.rollback()
                    return False
            conn.commit()
            golden.commit()
            return True
        finally:
            conn.close()
            golden.close()
    except Exception as e:
        logger.warning("replace_execution_fill_splits failed: %s", e)
        return False


def _raw_table_pk_for_account_executions_id(
    account_executions_id: int,
    *,
    golden: bool = True,
) -> Tuple[str, str, int]:
    """
    Map overlay account_executions_id to the row in executions_raw_flex | executions_raw_tws | executions_raw_journal.

    Encoding (same as account_executions view):
    - flex:    account_executions_id = executions_raw_flex_id  (> 0)
    - TWS:     account_executions_id = -executions_raw_tws_id  (negative, > -_JOURNAL_ID_OFFSET)
    - journal: account_executions_id = -(_JOURNAL_ID_OFFSET + executions_raw_journal_id)  (<= -_JOURNAL_ID_OFFSET)

    ``golden=True`` (default): physical tables on bifrost_golden_source (``raw_broker.*``).
    ``golden=False``: Trade-env FDW names (``brokerage.*``) for updatable foreign tables.
    """
    aid = int(account_executions_id)
    if aid > 0:
        return (
            (GOLDEN_EXECUTIONS_RAW_FLEX if golden else EXECUTIONS_RAW_FLEX),
            "executions_raw_flex_id",
            aid,
        )
    if aid <= -_JOURNAL_ID_OFFSET:
        return (
            (GOLDEN_EXECUTIONS_RAW_JOURNAL if golden else EXECUTIONS_RAW_JOURNAL),
            "executions_raw_journal_id",
            -aid - _JOURNAL_ID_OFFSET,
        )
    return (
        (GOLDEN_EXECUTIONS_RAW_TWS if golden else EXECUTIONS_RAW_TWS),
        "executions_raw_tws_id",
        -aid,
    )


def get_accounts_from_tables(
    conn: Any,
    *,
    include_position_exec_times: bool = True,
) -> Optional[List[Dict[str, Any]]]:
    """Load account snapshots.

    When ``include_position_exec_times`` is False (Live /status light path), also skip
    per-position stock-day fallback lookups and strategy_links exec scans — those were
    closing the StatusReader connection under statement_timeout and emptying Market Streams.
    """
    if conn is None:
        return None
    light = not include_position_exec_times
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT account_id, updated_at, net_liquidation, total_cash, buying_power, summary_extra FROM {ACCOUNT} ORDER BY account_id"
            )
            acc_rows = cur.fetchall()
        if not acc_rows:
            return []
        # The instrument class joins only where its table exists (0.27.0): an env
        # served by this core before the DDL reached its database reads every
        # position unclassified instead of failing the whole positions read.
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('preference_instrument_class') IS NOT NULL")
            has_instrument_class = bool(cur.fetchone()[0])
        ic_col = "ic.instrument_class" if has_instrument_class else "NULL::text AS instrument_class"
        ic_join = (
            "LEFT JOIN preference_instrument_class ic ON ic.contract_key = ap.contract_key"
            if has_instrument_class
            else ""
        )
        out: List[Dict[str, Any]] = []
        for row in acc_rows:
            acc_id = row.get("account_id") or ""
            summary: Dict[str, Any] = {}
            if row.get("net_liquidation") is not None:
                summary["NetLiquidation"] = str(row["net_liquidation"])
            if row.get("total_cash") is not None:
                summary["TotalCashValue"] = str(row["total_cash"])
            if row.get("buying_power") is not None:
                summary["BuyingPower"] = str(row["buying_power"])
            if acc_id:
                summary["account"] = acc_id
            extra = row.get("summary_extra")
            if isinstance(extra, dict):
                for k, v in extra.items():
                    summary[k] = v if isinstance(v, str) else str(v)
            _exec_tbl = _EXEC_READ_TABLE
            if include_position_exec_times:
                _exec_time_cols = f"""
                        (SELECT e.exec_time
                         FROM {_exec_tbl} e
                         WHERE e.account_id = ap.account_id
                           AND (
                             e.contract_key = ap.contract_key
                             OR (
                               upper(trim(COALESCE(ap.sec_type,''))) = 'OPT'
                               AND upper(trim(COALESCE(e.sec_type,''))) = 'OPT'
                               AND position('|' in e.contract_key) > 0
                               AND position('|' in ap.contract_key) > 0
                               AND (
                                 (CASE WHEN position(' ' in split_part(e.contract_key, '|', 1)) > 0
                                       THEN substring(split_part(e.contract_key, '|', 1) from 1 for position(' ' in split_part(e.contract_key, '|', 1)) - 1)
                                       ELSE split_part(e.contract_key, '|', 1)
                                  END) || substring(e.contract_key from position('|' in e.contract_key))
                                 ) = (
                                 (CASE WHEN position(' ' in split_part(ap.contract_key, '|', 1)) > 0
                                       THEN substring(split_part(ap.contract_key, '|', 1) from 1 for position(' ' in split_part(ap.contract_key, '|', 1)) - 1)
                                       ELSE split_part(ap.contract_key, '|', 1)
                                  END) || substring(ap.contract_key from position('|' in ap.contract_key))
                                 )
                             )
                           )
                         ORDER BY e.exec_time DESC NULLS LAST
                         LIMIT 1) AS position_exec_time,
                        (SELECT e.trade_date
                         FROM {_exec_tbl} e
                         WHERE e.account_id = ap.account_id
                           AND (
                             e.contract_key = ap.contract_key
                             OR (
                               upper(trim(COALESCE(ap.sec_type,''))) = 'OPT'
                               AND upper(trim(COALESCE(e.sec_type,''))) = 'OPT'
                               AND position('|' in e.contract_key) > 0
                               AND position('|' in ap.contract_key) > 0
                               AND (
                                 (CASE WHEN position(' ' in split_part(e.contract_key, '|', 1)) > 0
                                       THEN substring(split_part(e.contract_key, '|', 1) from 1 for position(' ' in split_part(e.contract_key, '|', 1)) - 1)
                                       ELSE split_part(e.contract_key, '|', 1)
                                  END) || substring(e.contract_key from position('|' in e.contract_key))
                                 ) = (
                                 (CASE WHEN position(' ' in split_part(ap.contract_key, '|', 1)) > 0
                                       THEN substring(split_part(ap.contract_key, '|', 1) from 1 for position(' ' in split_part(ap.contract_key, '|', 1)) - 1)
                                       ELSE split_part(ap.contract_key, '|', 1)
                                  END) || substring(ap.contract_key from position('|' in ap.contract_key))
                                 )
                             )
                           )
                         ORDER BY e.exec_time DESC NULLS LAST
                         LIMIT 1) AS position_trade_date,"""
            else:
                _exec_time_cols = """
                        NULL::timestamptz AS position_exec_time,
                        NULL::date AS position_trade_date,"""
            with conn.cursor(cursor_factory=RealDictCursor) as cur2:
                cur2.execute(
                    f"""
                    SELECT
                        ap.account_id,
                        ap.symbol,
                        ap.sec_type,
                        ap.exchange,
                        ap.currency,
                        ap.position,
                        ap.avg_cost,
                        ap.updated_at AS position_updated_at,
                        {_exec_time_cols}
                        ap.expiry,
                        ap.strike,
                        ap.option_right,
                        ap.contract_key,
                        pct.category_id AS position_category_id,
                        pc.name AS position_category_name,
                        {ic_col},
                        w.optionable AS watchlist_optionable
                    FROM {POSITIONS} ap
                    LEFT JOIN preference_position_category_tags pct
                        ON ap.account_id = pct.account_id AND ap.contract_key = pct.contract_key
                    LEFT JOIN preference_position_categories pc
                        ON pct.category_id = pc.id
                    {ic_join}
                    LEFT JOIN watchlist w
                        ON w.contract_key = ap.contract_key
                    WHERE ap.account_id = %s
                    ORDER BY ap.contract_key
                    """,
                    (acc_id,),
                )
                pos_rows = cur2.fetchall()
            positions = []
            for p in pos_rows:
                pos_dict: Dict[str, Any] = {
                    "account": p.get("account_id"),
                    "symbol": p.get("symbol") or "",
                    "secType": p.get("sec_type") or "",
                    "exchange": p.get("exchange") or "",
                    "currency": p.get("currency") or "",
                    "position": p.get("position"),
                    "avgCost": p.get("avg_cost"),
                    "contract_key": p.get("contract_key"),
                }
                if p.get("expiry") is not None:
                    pos_dict["lastTradeDateOrContractMonth"] = p.get("expiry")
                if p.get("strike") is not None:
                    pos_dict["strike"] = p.get("strike")
                if p.get("option_right") is not None:
                    pos_dict["right"] = p.get("option_right")

                cat_id = p.get("position_category_id")
                if cat_id is not None:
                    try:
                        pos_dict["category_id"] = int(cat_id)
                    except (TypeError, ValueError):
                        pass
                cat_name = p.get("position_category_name")
                if cat_name is not None and str(cat_name).strip():
                    pos_dict["category"] = str(cat_name).strip()

                # The Owner's registration (0.27.0); absent = unregistered, which callers read as a stock.
                inst_cls = p.get("instrument_class")
                if inst_cls:
                    pos_dict["instrument_class"] = str(inst_cls)

                wl_opt = p.get("watchlist_optionable")
                if wl_opt is not None:
                    pos_dict["optionable"] = bool(wl_opt)

                raw_pos_updated = p.get("position_updated_at")
                if raw_pos_updated is not None:
                    try:
                        if hasattr(raw_pos_updated, "timestamp"):
                            pos_dict["updated_at"] = raw_pos_updated.timestamp()
                        elif isinstance(raw_pos_updated, (int, float)) and math.isfinite(float(raw_pos_updated)):
                            pos_dict["updated_at"] = float(raw_pos_updated)
                    except (TypeError, ValueError):
                        pass
                raw_exec_time = p.get("position_exec_time")
                if raw_exec_time is not None:
                    try:
                        if hasattr(raw_exec_time, "timestamp"):
                            t = raw_exec_time.timestamp()
                        elif isinstance(raw_exec_time, (int, float)) and math.isfinite(float(raw_exec_time)):
                            t = float(raw_exec_time)
                        else:
                            t = None
                        if t is not None and math.isfinite(t):
                            pos_dict["exec_time"] = t
                    except (TypeError, ValueError):
                        pass
                raw_trade_date = p.get("position_trade_date")
                if raw_trade_date is not None:
                    try:
                        if hasattr(raw_trade_date, "isoformat"):
                            pos_dict["trade_date"] = raw_trade_date.isoformat()
                        elif isinstance(raw_trade_date, str) and raw_trade_date.strip():
                            pos_dict["trade_date"] = raw_trade_date.strip()[:10]
                    except (TypeError, ValueError):
                        pass

                # A stock's price is its last daily close from the market-data plugin, dated by
                # the bar. Live ticks are GET /quotes (Redis), which the pages overlay;
                # contract_quote_live has no writer (TD-260). An option, and a stock on the
                # light path or without a bar, carries no price and no unrealized_pnl --
                # absent, never 0.
                sec_typ = (p.get("sec_type") or "").strip().upper()
                price_for_pnl: Optional[float] = None
                if sec_typ == "STK" and not light:
                    fb = market_module.get_stock_day_fallback_price(conn, p.get("symbol") or "")
                    if fb is not None:
                        price_for_pnl = fb[0]
                        pos_dict["price"] = fb[0]
                        pos_dict["price_updated_at"] = fb[1]
                        # Latest bar today → prev_close is yesterday; otherwise bar close is yesterday.
                        dpc = resolve_daily_prev_close_from_fallback(fb[0], fb[1], fb[2])
                        if dpc is not None:
                            pos_dict["daily_prev_close"] = dpc
                pos_qty = p.get("position")
                pos_avg = p.get("avg_cost")
                sec_type = (p.get("sec_type") or "").strip().upper()
                # price_for_pnl is quoted per share; avg_cost arrives per contract
                # for options. Subtracting them directly and then multiplying by
                # the multiplier mixed units — currently masked because option
                # quotes are absent, which is not a reason to leave it.
                cost_per_share = option_cost_per_share(pos_avg, sec_type)
                if price_for_pnl is not None and pos_qty is not None and cost_per_share is not None:
                    pnl = position_value(price_for_pnl - cost_per_share, pos_qty, sec_type)
                    if pnl is not None:
                        pos_dict["unrealized_pnl"] = round(pnl, 2)

                positions.append(pos_dict)
            # Derive strategy_links from account_executions (one position may map to multiple strategies)
            ck_list = [pd.get("contract_key") for pd in positions if pd.get("contract_key")]
            strat_links_map: Dict[str, list] = {}
            if ck_list and not light:
                try:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur_sl:
                        cur_sl.execute(
                            f"""
                            SELECT u.contract_key,
                                   u.strategy_opportunity_id,
                                   u.trade_id,
                                   so.name AS strategy_opportunity_name,
                                   si.label AS trade_label
                            FROM (
                                SELECT DISTINCT contract_key, strategy_opportunity_id, trade_id
                                FROM (
                                    SELECT contract_key, strategy_opportunity_id, trade_id
                                    FROM {_exec_tbl}
                                    WHERE account_id = %s
                                      AND contract_key = ANY(%s::text[])
                                      AND (strategy_opportunity_id IS NOT NULL OR trade_id IS NOT NULL)
                                    UNION
                                    SELECT e.contract_key,
                                           si.strategy_opportunity_id,
                                           a.trade_id
                                    FROM {TRADE_FILL_SPLITS} a
                                    INNER JOIN trade si ON a.trade_id = si.trade_id
                                    INNER JOIN {_exec_tbl} e
                                      ON e.account_executions_id = a.account_executions_id
                                     AND e.account_id IS NOT DISTINCT FROM a.account_id
                                    WHERE a.account_id = %s
                                      AND e.contract_key = ANY(%s::text[])
                                ) x
                            ) u
                            LEFT JOIN strategy_opportunity so ON u.strategy_opportunity_id = so.strategy_opportunity_id
                            LEFT JOIN trade si ON u.trade_id = si.trade_id
                            """,
                            (acc_id, ck_list, acc_id, ck_list),
                        )
                        for sl_row in cur_sl.fetchall():
                            ck = sl_row.get("contract_key") or ""
                            link: Dict[str, Any] = {}
                            if sl_row.get("strategy_opportunity_id") is not None:
                                link["strategy_opportunity_id"] = int(sl_row["strategy_opportunity_id"])
                            if sl_row.get("trade_id") is not None:
                                link["trade_id"] = int(sl_row["trade_id"])
                            if sl_row.get("strategy_opportunity_name"):
                                link["strategy_opportunity_name"] = str(sl_row["strategy_opportunity_name"]).strip()
                            if sl_row.get("trade_label"):
                                link["trade_label"] = str(sl_row["trade_label"]).strip()
                            if link:
                                strat_links_map.setdefault(ck, []).append(link)
                except Exception as sl_err:
                    logger.debug("strategy_links derivation failed: %s", sl_err)
            for pd in positions:
                ck = pd.get("contract_key") or ""
                links = strat_links_map.get(ck, [])
                if links:
                    pd["strategy_links"] = links
            out.append({"account_id": acc_id, "summary": summary, "positions": positions})
        return out
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # Let StatusReader reconnect/retry — swallowing left Live Market Streams empty.
        raise
    except Exception as e:
        logger.warning("get_accounts_from_tables failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def get_accounts_fetched_at(conn: Any) -> Optional[float]:
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT max(updated_at) AS t FROM {ACCOUNT}")
            row = cur.fetchone()
        if row and row[0] is not None:
            ts = row[0]
            return ts.timestamp() if hasattr(ts, "timestamp") else float(ts)
        return None
    except Exception as e:
        logger.debug("get_accounts_fetched_at failed: %s", e)
        return None


# --- Module-level (status_config) write/CRUD for routers and __init__ re-exports ---


def sync_accounts_snapshot_to_db(
    status_config: dict, accounts_list: Optional[List[Dict[str, Any]]]
) -> bool:
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    if not accounts_list:
        return True
    try:
        conn = ws.open_conn(status_config, golden=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SET lock_timeout = '5s'")
            sync_accounts_snapshot_to_tables(conn, accounts_list)
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("sync_accounts_snapshot_to_db failed: %s", e)
        return False


def write_account_executions_to_db(
    status_config: dict,
    rows: List[Dict[str, Any]],
    *,
    stats_out: Optional[Dict[str, Any]] = None,
) -> bool:
    """R-A2: 写入执行记录到 account_executions；CommissionReport 写入 account_execution_commissions。按 exec_id 去重。

    If ``stats_out`` is provided, it is cleared and filled with TWS raw table stats (executions_raw_tws only):
    ``tws_raw_inserted``, ``tws_raw_skipped_duplicate``, ``tws_raw_missing_table`` (bool),
    ``tws_raw_inserted_ids``, ``tws_raw_updated_ids`` (always empty for TWS; flex uses upsert elsewhere),
    ``tws_raw_skipped_ids`` (existing ``executions_raw_tws_id`` when ``exec_id`` was duplicate).
    """
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    if stats_out is not None:
        stats_out.clear()
        stats_out["tws_raw_inserted"] = 0
        stats_out["tws_raw_skipped_duplicate"] = 0
        stats_out["tws_raw_missing_table"] = False
        stats_out["tws_raw_inserted_ids"] = []
        stats_out["tws_raw_updated_ids"] = []
        stats_out["tws_raw_skipped_ids"] = []
    try:
        conn = ws.open_conn(status_config, golden=True)
        try:
            with conn.cursor() as cur:
                for r in rows:
                    exec_id = r.get("exec_id")
                    account_id = r.get("account_id")
                    exec_time = r.get("time")
                    symbol = r.get("symbol")
                    sec_type = r.get("sec_type")
                    side = r.get("side")
                    quantity = r.get("quantity")
                    price = r.get("price")
                    source = r.get("source")
                    expiry = r.get("expiry")
                    strike = r.get("strike")
                    option_right = r.get("option_right")
                    exchange = r.get("exchange")
                    order_id = r.get("order_id")
                    cum_qty = r.get("cum_qty")
                    contract_key = r.get("contract_key")
                    currency = r.get("currency")
                    asset_category = r.get("asset_category")
                    sub_category = r.get("sub_category")
                    description = r.get("description")
                    conid = r.get("conid")
                    security_id = r.get("security_id")
                    security_id_type = r.get("security_id_type")
                    cusip = r.get("cusip")
                    isin = r.get("isin")
                    figi = r.get("figi")
                    listing_exchange = r.get("listing_exchange")
                    underlying_conid = r.get("underlying_conid")
                    underlying_symbol = r.get("underlying_symbol")
                    underlying_security_id = r.get("underlying_security_id")
                    underlying_listing_exchange = r.get("underlying_listing_exchange")
                    issuer = r.get("issuer")
                    issuer_country_code = r.get("issuer_country_code")
                    trade_id = r.get("trade_id")
                    related_trade_id = r.get("related_trade_id")
                    report_date = r.get("report_date")
                    # Flex Trades 去重：无 exec_id 时用 account_id+trade_id 合成 exec_id，使 ON CONFLICT 生效
                    if (
                        source == "flex_trades"
                        and (not exec_id or not str(exec_id).strip())
                        and account_id
                        and trade_id
                    ):
                        exec_id = f"flex_{account_id}_{trade_id}"
                    elif not exec_id or not str(exec_id).strip():
                        exec_id = None
                    trade_date = r.get("trade_date")
                    settle_date_target = r.get("settle_date_target")
                    transaction_type = r.get("transaction_type")
                    multiplier = r.get("multiplier")
                    principal_adjust_factor = r.get("principal_adjust_factor")
                    proceeds = r.get("proceeds")
                    taxes = r.get("taxes")
                    net_cash = r.get("net_cash")
                    close_price = r.get("close_price")
                    open_close_indicator = r.get("open_close_indicator")
                    notes = r.get("notes")
                    cost = r.get("cost")
                    fifo_pnl_realized = r.get("fifo_pnl_realized")
                    mtm_pnl = r.get("mtm_pnl")
                    trade_money = r.get("trade_money")
                    fx_rate_to_base = r.get("fx_rate_to_base")
                    acct_alias = r.get("acct_alias")
                    model = r.get("model")
                    raw_extra = r.get("raw_extra")
                    if raw_extra is not None and not isinstance(raw_extra, str):
                        raw_extra = json.dumps(raw_extra) if raw_extra else None

                    # TWS option executions: rebuild the key from the legacy local symbol
                    # (SYMBOL  YYMMDDR########|OPT|YYYYMMDD|strike|R; portfolio.contract_key).
                    if (sec_type or "").strip().upper() == "OPT" and (source or "").strip() in TWS_SOURCES:
                        contract_key = (
                            tws_execution_opt_key(symbol, expiry, strike, option_right) or contract_key
                        )

                    if exec_time is not None:
                        try:
                            if isinstance(exec_time, (int, float)):
                                exec_dt = datetime.fromtimestamp(float(exec_time), tz=timezone.utc)
                            else:
                                exec_dt = exec_time
                        except Exception:
                            exec_dt = None
                    else:
                        exec_dt = None
                    # When source is not flex_trades, trade_date is not provided by the source; set it from exec_time.
                    if (source or "").strip() != "flex_trades" and trade_date is None and exec_dt is not None:
                        try:
                            trade_date = exec_dt.date() if hasattr(exec_dt, "date") else None
                        except Exception:
                            trade_date = None
                    cols = (
                        "account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, "
                        "expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, "
                        "asset_category, sub_category, description, conid, security_id, security_id_type, "
                        "cusip, isin, figi, listing_exchange, underlying_conid, underlying_symbol, "
                        "underlying_security_id, underlying_listing_exchange, issuer, issuer_country_code, "
                        "trade_id, related_trade_id, report_date, trade_date, settle_date_target, "
                        "transaction_type, multiplier, principal_adjust_factor, proceeds, taxes, net_cash, "
                        "close_price, open_close_indicator, notes, cost, fifo_pnl_realized, mtm_pnl, "
                        "trade_money, fx_rate_to_base, acct_alias, model, raw_extra"
                    )
                    placeholders = ", ".join(["%s"] * 54)
                    vals = (
                        account_id,
                        exec_id,
                        exec_dt,
                        symbol,
                        sec_type,
                        side,
                        quantity,
                        price,
                        source,
                        expiry,
                        strike,
                        option_right,
                        exchange,
                        order_id,
                        cum_qty,
                        contract_key,
                        asset_category,
                        sub_category,
                        description,
                        conid,
                        security_id,
                        security_id_type,
                        cusip,
                        isin,
                        figi,
                        listing_exchange,
                        underlying_conid,
                        underlying_symbol,
                        underlying_security_id,
                        underlying_listing_exchange,
                        issuer,
                        issuer_country_code,
                        trade_id,
                        related_trade_id,
                        report_date,
                        trade_date,
                        settle_date_target,
                        transaction_type,
                        multiplier,
                        principal_adjust_factor,
                        proceeds,
                        taxes,
                        net_cash,
                        close_price,
                        open_close_indicator,
                        notes,
                        cost,
                        fifo_pnl_realized,
                        mtm_pnl,
                        trade_money,
                        fx_rate_to_base,
                        acct_alias,
                        model,
                        raw_extra,
                    )
                    # ── Write to source-split raw tables (raw_broker.* on Golden Source) ──
                    try:
                        is_flex_source = (source == "flex_trades")
                        is_journal_source = (source == "journal_closed")
                        if is_flex_source:
                            raw_table = GOLDEN_EXECUTIONS_RAW_FLEX
                        elif is_journal_source:
                            raw_table = GOLDEN_EXECUTIONS_RAW_JOURNAL
                        else:
                            raw_table = GOLDEN_EXECUTIONS_RAW_TWS
                        if exec_id:
                            if is_flex_source:
                                raw_update_set = ", ".join(
                                    f"{c.strip()} = EXCLUDED.{c.strip()}" for c in cols.split(",")
                                )
                                cur.execute(
                                    f"""
                                    INSERT INTO {raw_table} ({cols})
                                    VALUES ({placeholders})
                                    ON CONFLICT (exec_id) WHERE exec_id IS NOT NULL AND exec_id != ''
                                    DO UPDATE SET {raw_update_set}
                                    """,
                                    vals,
                                )
                            else:
                                _ret = (
                                    "\nRETURNING executions_raw_tws_id"
                                    if raw_table == GOLDEN_EXECUTIONS_RAW_TWS
                                    else ""
                                )
                                cur.execute(
                                    f"""
                                    INSERT INTO {raw_table} ({cols})
                                    VALUES ({placeholders})
                                    ON CONFLICT (exec_id) WHERE exec_id IS NOT NULL AND exec_id != '' DO NOTHING{_ret}
                                    """,
                                    vals,
                                )
                                if stats_out is not None and raw_table == GOLDEN_EXECUTIONS_RAW_TWS:
                                    ins_row = cur.fetchone()
                                    if ins_row and ins_row[0] is not None:
                                        stats_out["tws_raw_inserted"] += 1
                                        stats_out["tws_raw_inserted_ids"].append(int(ins_row[0]))
                                    else:
                                        stats_out["tws_raw_skipped_duplicate"] += 1
                                        cur.execute(
                                            f"""
                                            SELECT executions_raw_tws_id
                                            FROM {GOLDEN_EXECUTIONS_RAW_TWS}
                                            WHERE exec_id = %s
                                            LIMIT 1
                                            """,
                                            (exec_id,),
                                        )
                                        sk = cur.fetchone()
                                        if sk and sk[0] is not None:
                                            stats_out["tws_raw_skipped_ids"].append(int(sk[0]))
                        else:
                            _ret_ins = (
                                "\nRETURNING executions_raw_tws_id"
                                if raw_table == GOLDEN_EXECUTIONS_RAW_TWS
                                else ""
                            )
                            cur.execute(
                                f"""
                                INSERT INTO {raw_table} ({cols})
                                VALUES ({placeholders}){_ret_ins}
                                """,
                                vals,
                            )
                            if stats_out is not None and raw_table == GOLDEN_EXECUTIONS_RAW_TWS:
                                ins_row = cur.fetchone() if _ret_ins else None
                                if ins_row and ins_row[0] is not None:
                                    stats_out["tws_raw_inserted"] += 1
                                    stats_out["tws_raw_inserted_ids"].append(int(ins_row[0]))
                    except Exception as _raw_e:
                        # Older deployments without split raw tables: ignore missing relation only.
                        if getattr(_raw_e, "pgcode", None) == "42P01":
                            if stats_out is not None:
                                stats_out["tws_raw_missing_table"] = True
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                        else:
                            logger.warning(
                                "write_account_executions_to_db: raw insert failed table=%s exec_id=%r: %s",
                                raw_table,
                                exec_id,
                                _raw_e,
                                exc_info=True,
                            )
                            raise

                    commission = r.get("commission")
                    realized_pnl = r.get("realized_pnl")
                    currency = r.get("currency")
                    yield_ = r.get("yield_")
                    yield_redemption_date = r.get("yield_redemption_date")
                    # 仅当有至少一个「有意义」的 commission 字段时才写 commission 表，避免 7 天拉取时用空数据覆盖 1 天拉到的有效值
                    has_comm = (
                        _has_meaningful_commission(commission)
                        or _has_meaningful_commission(realized_pnl)
                        or _has_meaningful_commission(currency, is_numeric=False)
                        or _has_meaningful_commission(yield_)
                        or _has_meaningful_commission(yield_redemption_date)
                    )
                    if exec_id and has_comm:
                        # Flex keeps IB's statement sign; TWS / gateway reports are cost-positive (TD-114).
                        upsert_commission(
                            cur,
                            exec_id,
                            commission=stored_commission(commission, source),
                            currency=currency,
                            realized_pnl=realized_pnl,
                            yield_=yield_,
                            yield_redemption_date=yield_redemption_date,
                        )
            n_comm = sum(1 for r in rows if r.get("exec_id") and (r.get("commission") is not None or r.get("realized_pnl") is not None or r.get("currency") is not None or r.get("yield_") is not None or r.get("yield_redemption_date") is not None))
            conn.commit()
            logger.info("[R-A2] write_account_executions_to_db: wrote %s rows (%s with commission)", len(rows), n_comm)
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_account_executions_to_db failed: %s", e)
        return False


def update_execution_commission(
    status_config: dict,
    exec_id: str,
    commission: Optional[float],
    realized_pnl: Optional[float],
    currency: Optional[str],
    yield_: Optional[float] = None,
    yield_redemption_date: Optional[int] = None,
) -> bool:
    """R-A2: 收到 IB commissionReport 事件时按 exec_id 写入 account_execution_commissions。

    ``commission`` is the IB API's cost-positive value; it is stored in IB's statement sign
    (negated), the sign Flex stores for the same fill (TD-114).
    """
    if not exec_id or not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        conn = ws.open_conn(status_config, golden=True)
        try:
            with conn.cursor() as cur:
                upsert_commission(
                    cur,
                    exec_id,
                    commission=stored_commission(commission, None),
                    currency=currency,
                    realized_pnl=realized_pnl,
                    yield_=yield_,
                    yield_redemption_date=yield_redemption_date,
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("update_execution_commission failed: exec_id=%r %s", exec_id, e)
        return False


def insert_one_execution(status_config: dict, body: Dict[str, Any]) -> Optional[int]:
    """R-A2 扩展：手动添加一条执行记录（历史补录）。返回新行 account_executions_id（与 account_executions 视图一致），失败返回 None。
    body: account_id, time(Unix s), symbol, sec_type, side, quantity, price; 可选 source('manual'|'journal_closed'), …
    source='journal_closed' 时仅写入 executions_raw_journal，返回 -(1e9 + executions_raw_journal_id)。
    若未提供 exec_id 则生成 manual_<uuid> 以便可写 commission 表。"""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    # trade_id / fill_splits only (strategy_instance_id / instance_allocations before core 0.47.0, naming R4).
    account_id = body.get("account_id") or ""
    exec_time = body.get("time")
    symbol = (body.get("symbol") or "").strip()
    sec_type = (body.get("sec_type") or "STK").strip().upper() or "STK"
    side = (body.get("side") or "").strip().upper()
    quantity = body.get("quantity")
    price = body.get("price")
    if symbol is None or quantity is None or price is None:
        return None
    exec_id = (body.get("exec_id") or "").strip()
    if not exec_id:
        exec_id = "manual_" + uuid.uuid4().hex
    source = (body.get("source") or "manual").strip() or "manual"
    expiry = body.get("expiry")
    strike = body.get("strike")
    option_right = body.get("option_right")
    exchange = body.get("exchange")
    order_id = body.get("order_id")
    cum_qty = body.get("cum_qty")
    contract_key = body.get("contract_key")
    raw_extra = body.get("raw_extra")
    if raw_extra is not None and not isinstance(raw_extra, str):
        raw_extra = json.dumps(raw_extra) if raw_extra else None
    try:
        _, trade_id, strategy_opportunity_id = _direct_instance(body)
    except (TypeError, ValueError):
        return None
    if trade_id is None and strategy_opportunity_id is not None:
        logger.warning("insert_one_execution refused: %s", OPPORTUNITY_ONLY)
        return None
    exec_dt = _exec_time_to_dt(exec_time)
    try:
        env_conn = ws.open_conn(status_config)
        golden = ws.open_conn(status_config, golden=True)
        try:
            with golden.cursor() as cur, env_conn.cursor() as env_cur:
                # The strategy attribution goes to this env's trade_execution
                # below (TD-09), not to Golden Source's strategy_* columns.
                cols = "account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, raw_extra"
                placeholders = "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
                vals = (account_id, exec_id, exec_dt, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, raw_extra)
                new_id = None
                # Write physical raw tables on golden_source (executions view is read-only).
                if source == "journal_closed":
                    cur.execute(
                        f"""
                        INSERT INTO {GOLDEN_EXECUTIONS_RAW_JOURNAL} ({cols})
                        VALUES ({placeholders})
                        RETURNING executions_raw_journal_id
                        """,
                        vals,
                    )
                    row = cur.fetchone()
                    raw_jid = row[0] if row else None
                    if raw_jid is not None:
                        new_id = -(1000000000 + int(raw_jid))
                else:
                    cur.execute(
                        f"""
                        INSERT INTO {GOLDEN_EXECUTIONS_RAW_TWS} ({cols})
                        VALUES ({placeholders})
                        RETURNING executions_raw_tws_id
                        """,
                        vals,
                    )
                    row = cur.fetchone()
                    raw_tid = row[0] if row else None
                    if raw_tid is not None:
                        new_id = -int(raw_tid)
                commission = body.get("commission")
                realized_pnl = body.get("realized_pnl")
                currency = body.get("currency")
                if commission is not None or realized_pnl is not None or (currency and str(currency).strip()):
                    # The form's commission is cost-positive; stored in IB's statement sign (TD-114).
                    upsert_commission(
                        cur,
                        exec_id,
                        commission=stored_commission(commission, None),
                        currency=currency,
                        realized_pnl=realized_pnl,
                        zero_keeps_existing=False,
                    )
                if new_id is not None and trade_id is not None:
                    acc_key = str(account_id or "").strip()
                    problem = _instance_problem(env_cur, trade_id, acc_key, strategy_opportunity_id)
                    if problem is not None:
                        logger.warning("insert_one_execution refused: %s", problem)
                        env_conn.rollback()
                        golden.rollback()
                        return None
                    _set_whole_attribution(env_cur, acc_key, exec_id, trade_id)
                if new_id is not None and body.get("fill_splits") is not None:
                    ia = body.get("fill_splits")
                    if not isinstance(ia, list):
                        env_conn.rollback()
                        golden.rollback()
                        return None
                    rtbl, rpkc, rpkv = _raw_table_pk_for_account_executions_id(new_id)
                    if not _apply_fill_splits_on_cursor(
                        env_cur, new_id, rtbl, rpkc, rpkv, ia, raw_cur=cur
                    ):
                        env_conn.rollback()
                        golden.rollback()
                        return None
            golden.commit()
            env_conn.commit()
            return new_id
        finally:
            env_conn.close()
            golden.close()
    except Exception as e:
        logger.warning("insert_one_execution failed: %s", e)
        return None



def upsert_account_transactions(status_config: dict, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Insert or update account_transactions from Flex cash transaction list.

    Returns ``(written, skipped)``. Rows missing account_id, a numeric ts, or report_date
    are skipped and not written. Database errors propagate: a lost grant, lock timeout,
    or bad value is not reported as zero rows written (TD-91).
    Each row at minimum: account_id, ts (Unix float), amount, type, currency?, description?.
    Extended fields (when present): flex_transaction_id, flex_type, flex_code, asset_category, asset_subcategory,
    symbol, conid, security_id, security_id_type, listing_exchange, report_date, available_for_trading_date,
    fx_rate_to_base, raw_extra.
    A row with flex_transaction_id conflicts on
    (account_id, flex_transaction_id) WHERE flex_transaction_id IS NOT NULL
    (partial unique index transactions_account_flex_tx_uidx, which this function does not create).
    The second write replaces ts, amount, type, and report_date. A row without a transaction id
    still conflicts on (account_id, ts, amount, type, report_date)."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return (0, 0)
    if not rows:
        return (0, 0)
    conn = ws.open_conn(status_config, golden=True)
    written = 0
    skipped = 0
    enrich_set = ",\n".join(
        (
            f"currency = COALESCE(EXCLUDED.currency, {GOLDEN_TRANSACTIONS}.currency)",
            f"description = COALESCE(EXCLUDED.description, {GOLDEN_TRANSACTIONS}.description)",
            f"flex_transaction_id = COALESCE(EXCLUDED.flex_transaction_id, {GOLDEN_TRANSACTIONS}.flex_transaction_id)",
            f"flex_type = COALESCE(EXCLUDED.flex_type, {GOLDEN_TRANSACTIONS}.flex_type)",
            f"flex_code = COALESCE(EXCLUDED.flex_code, {GOLDEN_TRANSACTIONS}.flex_code)",
            f"asset_category = COALESCE(EXCLUDED.asset_category, {GOLDEN_TRANSACTIONS}.asset_category)",
            f"asset_subcategory = COALESCE(EXCLUDED.asset_subcategory, {GOLDEN_TRANSACTIONS}.asset_subcategory)",
            f"symbol = COALESCE(EXCLUDED.symbol, {GOLDEN_TRANSACTIONS}.symbol)",
            f"conid = COALESCE(EXCLUDED.conid, {GOLDEN_TRANSACTIONS}.conid)",
            f"security_id = COALESCE(EXCLUDED.security_id, {GOLDEN_TRANSACTIONS}.security_id)",
            f"security_id_type = COALESCE(EXCLUDED.security_id_type, {GOLDEN_TRANSACTIONS}.security_id_type)",
            f"listing_exchange = COALESCE(EXCLUDED.listing_exchange, {GOLDEN_TRANSACTIONS}.listing_exchange)",
            f"available_for_trading_date = COALESCE(EXCLUDED.available_for_trading_date, {GOLDEN_TRANSACTIONS}.available_for_trading_date)",
            f"fx_rate_to_base = COALESCE(EXCLUDED.fx_rate_to_base, {GOLDEN_TRANSACTIONS}.fx_rate_to_base)",
            f"raw_extra = COALESCE(EXCLUDED.raw_extra, {GOLDEN_TRANSACTIONS}.raw_extra)",
        )
    )
    insert_sql = f"""
                    INSERT INTO {GOLDEN_TRANSACTIONS} (
                        account_id, ts, amount, type, currency, description,
                        flex_transaction_id, flex_type, flex_code,
                        asset_category, asset_subcategory,
                        symbol, conid, security_id, security_id_type,
                        listing_exchange, report_date, available_for_trading_date,
                        fx_rate_to_base, raw_extra
                    )
                    VALUES (
                        %s, to_timestamp(%s), %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s
                    )
"""
    # The partial predicate must match transactions_account_flex_tx_uidx. Postgres
    # rejects this ON CONFLICT until that index exists.
    sql_flex_id = insert_sql + f"""
                    ON CONFLICT (account_id, flex_transaction_id) WHERE flex_transaction_id IS NOT NULL DO UPDATE SET
                        ts = EXCLUDED.ts,
                        amount = EXCLUDED.amount,
                        type = EXCLUDED.type,
                        report_date = EXCLUDED.report_date,
                        {enrich_set}
"""
    sql_legacy = insert_sql + f"""
                    ON CONFLICT (account_id, ts, amount, type, report_date) DO UPDATE SET
                        {enrich_set}
"""
    try:
        with conn.cursor() as cur:
            for r in rows:
                account_id = (r.get("account_id") or "").strip()
                ts = r.get("ts")
                amount = r.get("amount")
                tx_type = (r.get("type") or "other").strip() or "other"
                currency = (r.get("currency") or "").strip() or None
                description = (r.get("description") or "").strip() or None
                if not account_id:
                    skipped += 1
                    continue
                if ts is None:
                    skipped += 1
                    continue
                try:
                    ts_float = float(ts)
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                if amount is None:
                    amount = 0.0
                try:
                    amount_float = float(amount)
                except (TypeError, ValueError):
                    amount_float = 0.0

                flex_transaction_id = (r.get("flex_transaction_id") or "").strip() or None
                flex_type = (r.get("flex_type") or "").strip() or None
                flex_code = (r.get("flex_code") or "").strip() or None
                asset_category = (r.get("asset_category") or "").strip() or None
                asset_subcategory = (r.get("asset_subcategory") or "").strip() or None
                symbol = (r.get("symbol") or "").strip() or None
                conid = r.get("conid")
                try:
                    conid_int = int(conid) if conid is not None else None
                except (TypeError, ValueError):
                    conid_int = None
                security_id = (r.get("security_id") or "").strip() or None
                security_id_type = (r.get("security_id_type") or "").strip() or None
                listing_exchange = (r.get("listing_exchange") or "").strip() or None
                report_date = (r.get("report_date") or "").strip() or None
                # Wave 3 D-W3.3: UNIQUE includes report_date — skip unindexable rows.
                if not report_date:
                    logger.warning(
                        "upsert_account_transactions: skip row without report_date "
                        "(account_id=%s ts=%s amount=%s type=%s)",
                        account_id,
                        ts_float,
                        amount_float,
                        tx_type,
                    )
                    skipped += 1
                    continue
                available_for_trading_date = (r.get("available_for_trading_date") or "").strip() or None
                fx_rate_to_base = r.get("fx_rate_to_base")
                try:
                    fx_rate_to_base_float = float(fx_rate_to_base) if fx_rate_to_base is not None else None
                except (TypeError, ValueError):
                    fx_rate_to_base_float = None
                raw_extra = r.get("raw_extra")

                cur.execute(
                    sql_flex_id if flex_transaction_id else sql_legacy,
                    (
                        account_id,
                        ts_float,
                        amount_float,
                        tx_type,
                        currency,
                        description,
                        flex_transaction_id,
                        flex_type,
                        flex_code,
                        asset_category,
                        asset_subcategory,
                        symbol,
                        conid_int,
                        security_id,
                        security_id_type,
                        listing_exchange,
                        report_date,
                        available_for_trading_date,
                        fx_rate_to_base_float,
                        json.dumps(raw_extra) if raw_extra is not None else None,
                    ),
                )
                written += 1
            conn.commit()
            return (written, skipped)
    finally:
        conn.close()


def update_one_execution(status_config: dict, account_executions_id: int, body: Dict[str, Any]) -> bool:
    """R-A2 扩展：按 account_executions_id 更新一条执行记录（手动修正）。写入物理表 executions_raw_flex / executions_raw_tws / executions_raw_journal（与 account_executions 视图编码一致）；不可 UPDATE 联合视图本身。body 可含任意子集：time, symbol, … strategy_opportunity_id, trade_id；以及 commission, realized_pnl, currency（写 account_execution_commissions）。"""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    # trade_id / fill_splits only (strategy_instance_id / instance_allocations before core 0.47.0, naming R4).
    # 可更新列（与 raw 表一致）
    # strategy_opportunity_id / trade_id go to this env's
    # trade_execution (TD-09), not to the raw row.
    exec_cols = ("exec_time", "symbol", "sec_type", "side", "quantity", "price", "account_id", "source", "expiry", "strike", "option_right", "exchange", "order_id", "cum_qty", "contract_key")
    commission_keys = ("commission", "realized_pnl", "currency")
    updates: List[str] = []
    values: List[Any] = []
    for k in exec_cols:
        if k == "exec_time":
            # 前端传 time（Unix 秒），后端列名为 exec_time
            v = body.get("exec_time") if body.get("exec_time") is not None else body.get("time")
            if v is None:
                continue
            v = _exec_time_to_dt(v)
        elif k not in body:
            continue
        else:
            v = body[k]
        if k == "raw_extra" and v is not None and not isinstance(v, str):
            v = json.dumps(v) if v else None
        updates.append(f'"{k}" = %s')
        values.append(v)
    try:
        touches_whole, direct_instance, direct_opportunity = _direct_instance(body)
    except (TypeError, ValueError):
        touches_whole, direct_instance, direct_opportunity = True, None, None
    if direct_instance is None and direct_opportunity is not None:
        logger.warning("update_one_execution refused: %s", OPPORTUNITY_ONLY)
        return False
    raw_tbl, pk_col, pk_val = _raw_table_pk_for_account_executions_id(account_executions_id)
    values.append(pk_val)
    try:
        env_conn = ws.open_conn(status_config)
        golden = ws.open_conn(status_config, golden=True)
        try:
            with golden.cursor() as cur, env_conn.cursor() as env_cur:
                key = _fill_key(cur, raw_tbl, pk_col, pk_val, lock=True)
                if key is None:
                    golden.rollback()
                    env_conn.rollback()
                    return False
                old_account, exec_id_key, _ = key
                new_account = str(body["account_id"]).strip() if body.get("account_id") is not None else old_account
                if new_account != old_account and exec_id_key:
                    # The attribution is keyed by (account_id, exec_id) and its instance is
                    # on the old account: moving the fill would orphan it.
                    env_cur.execute(
                        f"SELECT 1 FROM {TRADE_EXECUTION} WHERE account_id = %s AND exec_id = %s LIMIT 1",
                        (old_account, exec_id_key),
                    )
                    if env_cur.fetchone():
                        logger.warning(
                            "update_one_execution refused: %s is attributed; clear its trade before moving it to %s",
                            account_executions_id,
                            new_account,
                        )
                        golden.rollback()
                        env_conn.rollback()
                        return False
                if updates:
                    cur.execute(
                        f"UPDATE {raw_tbl} SET " + ", ".join(updates) + f" WHERE {pk_col} = %s",
                        values,
                    )
                    if cur.rowcount == 0:
                        golden.rollback()
                        env_conn.rollback()
                        logger.warning(
                            "update_one_execution: no row in %s for %s=%s (account_executions_id=%s)",
                            raw_tbl,
                            pk_col,
                            pk_val,
                            account_executions_id,
                        )
                        return False
                elif (
                    not any(k in body for k in commission_keys)
                    and "fill_splits" not in body
                    and not touches_whole
                ):
                    return False
                if "fill_splits" in body:
                    ia = body.get("fill_splits")
                    if ia is not None and not isinstance(ia, list):
                        golden.rollback()
                        env_conn.rollback()
                        return False
                    if ia is not None:
                        if not _apply_fill_splits_on_cursor(
                            env_cur, account_executions_id, raw_tbl, pk_col, pk_val, ia, raw_cur=cur
                        ):
                            golden.rollback()
                            env_conn.rollback()
                            return False
                if touches_whole:
                    if not exec_id_key:
                        golden.rollback()
                        env_conn.rollback()
                        return False
                    if direct_instance is not None:
                        problem = _instance_problem(env_cur, direct_instance, new_account, direct_opportunity)
                        if problem is None and body.get("fill_splits"):
                            problem = "send fill_splits or trade_id, not both"
                        if problem is None and "fill_splits" not in body:
                            if _split_count(env_cur, new_account, exec_id_key):
                                problem = "the fill is split across trades; send fill_splits: []"
                        if problem is not None:
                            logger.warning("update_one_execution refused: %s", problem)
                            golden.rollback()
                            env_conn.rollback()
                            return False
                    _set_whole_attribution(env_cur, new_account, exec_id_key, direct_instance)
                # commission 相关（exec_id 从物理表读取）
                if any(k in body for k in commission_keys):
                    cur.execute(f"SELECT exec_id FROM {raw_tbl} WHERE {pk_col} = %s", (pk_val,))
                    row = cur.fetchone()
                    exec_id = row[0] if row and row[0] and str(row[0]).strip() else None
                    if not exec_id:
                        exec_id = "manual_" + str(account_executions_id)
                        cur.execute(
                            f'UPDATE {raw_tbl} SET exec_id = %s WHERE {pk_col} = %s',
                            (exec_id, pk_val),
                        )
                    # The form's commission is cost-positive (what the readers return), whatever
                    # the row's source; stored in IB's statement sign (TD-114).
                    upsert_commission(
                        cur,
                        exec_id,
                        commission=stored_commission(body.get("commission"), None),
                        currency=body.get("currency"),
                        realized_pnl=body.get("realized_pnl"),
                        zero_keeps_existing=False,
                    )
            golden.commit()
            env_conn.commit()
            return True
        finally:
            env_conn.close()
            golden.close()
    except Exception as e:
        logger.warning("update_one_execution failed: account_executions_id=%s %s", account_executions_id, e)
        return False



def delete_one_execution(status_config: dict, account_executions_id: int) -> bool:
    """R-A2 扩展：按 account_executions_id 删除一条执行记录。删除 golden raw 行 + commissions；无孪生行时清理本环境 trade_execution。"""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    raw_tbl, pk_col, pk_val = _raw_table_pk_for_account_executions_id(account_executions_id)
    try:
        env_conn = ws.open_conn(status_config)
        golden = ws.open_conn(status_config, golden=True)
        try:
            with golden.cursor() as cur, env_conn.cursor() as env_cur:
                key = _fill_key(cur, raw_tbl, pk_col, pk_val)
                exec_id = key[1] if key and key[1] else None
                if exec_id:
                    cur.execute(f"DELETE FROM {GOLDEN_COMMISSIONS} WHERE exec_id = %s", (exec_id,))
                cur.execute(f"DELETE FROM {raw_tbl} WHERE {pk_col} = %s", (pk_val,))
                if cur.rowcount == 0:
                    golden.rollback()
                    env_conn.rollback()
                    return False
                if key:
                    _drop_attribution_if_last(cur, env_cur, key[0], key[1])
            golden.commit()
            env_conn.commit()
            return True
        finally:
            env_conn.close()
            golden.close()
    except Exception as e:
        logger.warning("delete_one_execution failed: account_executions_id=%s %s", account_executions_id, e)
        return False



# --- TD-15 writers (core 0.33.0): return what was written / raise Write* ----------------
#
# An execution spans two databases: its raw row on Golden Source (raw_broker.*) and its
# strategy attribution -- whole fill or splits -- in this env's trade_execution
# (TD-09). Both writers below hold one transaction on each, check everything before
# writing, and commit Golden Source first, as update_one_execution / delete_one_execution do.

EXECUTION_PATCHABLE = ("strategy_opportunity_id", "trade_id", "fill_splits")
_GOLDEN_RAW_TABLES = (
    GOLDEN_EXECUTIONS_RAW_TWS,
    GOLDEN_EXECUTIONS_RAW_FLEX,
    GOLDEN_EXECUTIONS_RAW_JOURNAL,
)


def _execution_attribution(env_cur: Any, account_executions_id: int, account_id: str, exec_id: str) -> Dict[str, Any]:
    """The attribution fields of one execution, as GET /executions items carry them."""
    env_cur.execute(
        f"""
        SELECT sie.trade_id, sie.split_quantity, si.label, si.strategy_opportunity_id
        FROM {TRADE_EXECUTION} sie
        LEFT JOIN trade si ON si.trade_id = sie.trade_id
        WHERE sie.account_id = %s AND sie.exec_id = %s
        ORDER BY sie.split_quantity IS NOT NULL, sie.trade_id
        """,
        (account_id, exec_id),
    )
    whole: Optional[Tuple[int, Optional[int]]] = None
    allocations = []
    for r in env_cur.fetchall() or []:
        opp = int(r[3]) if r[3] is not None else None
        if r[1] is None:
            whole = (int(r[0]), opp)
            continue
        item: Dict[str, Any] = {
            "trade_id": int(r[0]),
            "quantity": float(r[1]),
            "strategy_opportunity_id": opp,
        }
        if r[2] is not None and str(r[2]).strip():
            item["trade_label"] = str(r[2]).strip()
        allocations.append(item)
    return {
        "account_executions_id": int(account_executions_id),
        "account_id": account_id,
        "strategy_opportunity_id": whole[1] if whole else None,
        "trade_id": whole[0] if whole else None,
        "fill_splits": allocations,
    }


def patch_execution(status_config: Any, account_executions_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change an execution's strategy attribution; return its attribution fields.

    Returns ``{account_executions_id, account_id, strategy_opportunity_id, trade_id,
    fill_splits: [{trade_id, quantity, strategy_opportunity_id,
    trade_label?}]}`` -- the attribution keys of a GET /executions item (the instance names
    beside them before core 0.47.0, naming R4).

    Patchable: ``trade_id`` (null clears the whole-fill attribution),
    ``strategy_opportunity_id`` (not stored -- it is the instance's: sent with an instance it
    must match it, sent alone it is WriteInvalid unless null) and ``fill_splits``
    (the splits, replaced whole; ``[]`` removes them). An execution is attributed one way or
    the other: non-empty splits together with an instance is WriteInvalid; setting an
    instance on an execution that has splits, without ``fill_splits: []`` in the
    same patch, is WriteConflict. The instance must exist and be on the execution's account,
    and splits must name distinct instances of that account and add up to the execution's
    quantity (WriteInvalid otherwise). The write goes to this env's
    trade_execution by (account_id, exec_id), so a TWS row and its Flex twin
    change together. The fill's own columns (time, price, quantity ...) are not patchable
    here: ``update_one_execution`` keeps them. Needs the status config (two databases).
    Raises WriteInvalid, WriteNotFound, WriteConflict, WriteFailed.
    """
    what = f"execution {account_executions_id}"
    # strategy_instance_id / instance_allocations are unknown keys since core 0.47.0 (naming R4).
    fields = ws.check_fields(fields, EXECUTION_PATCHABLE, "execution")
    direct: Dict[str, Any] = {}
    for name in ("strategy_opportunity_id", "trade_id"):
        if name in fields:
            direct[name] = ws.row_id(fields[name], name, nullable=True)
    splits: Optional[List[Dict[str, Any]]] = None
    if "fill_splits" in fields:
        splits = ws.list_value(fields["fill_splits"], "fill_splits")
        if any(not isinstance(item, dict) for item in splits):
            raise WriteInvalid("fill_splits must be a list of {trade_id, quantity}.")
    touches_whole, instance_id, opportunity_id = _direct_instance(direct)
    if instance_id is None and opportunity_id is not None:
        raise WriteInvalid(OPPORTUNITY_ONLY)
    if splits and instance_id is not None:
        raise WriteInvalid(
            "A fill is attributed one way or the other: send fill_splits, "
            "or trade_id, not both."
        )
    if not isinstance(status_config, dict):
        raise WriteFailed(f"Cannot write {what}: the status config is needed (it spans two databases).")
    raw_tbl, pk_col, pk_val = _raw_table_pk_for_account_executions_id(account_executions_id)
    with ws.write_connection(status_config, what) as env, ws.write_connection(status_config, what, golden=True) as golden:
        try:
            with golden.cursor() as gcur, env.cursor() as ecur:
                key = _fill_key(gcur, raw_tbl, pk_col, pk_val, lock=True)
                if key is None:
                    raise WriteNotFound(f"No execution {account_executions_id}.")
                account_id, exec_id, _ = key
                if not exec_id and (touches_whole or splits is not None):
                    raise WriteInvalid(f"Execution {account_executions_id} has no exec_id; it cannot be attributed.")
                if instance_id is not None:
                    problem = _instance_problem(ecur, instance_id, account_id, opportunity_id)
                    if problem is not None:
                        raise WriteInvalid(problem)
                    if splits is None:
                        n = _split_count(ecur, account_id, exec_id)
                        if n:
                            raise WriteConflict(
                                f"This fill is split across {ws.plural(n, 'trade', 'trades')}; "
                                "send fill_splits: [] with the trade to replace the split."
                            )
                if splits is not None:
                    if not _apply_fill_splits_on_cursor(
                        ecur, account_executions_id, raw_tbl, pk_col, pk_val, splits, raw_cur=gcur
                    ):
                        raise WriteInvalid(
                            "fill_splits must name distinct trades of account "
                            f"{account_id}, each with a non-zero quantity, adding up to the fill's quantity."
                        )
                if touches_whole:
                    _set_whole_attribution(ecur, account_id, exec_id, instance_id)
                out = _execution_attribution(ecur, account_executions_id, account_id, exec_id)
            golden.commit()
            env.commit()
            return out
        except Exception as e:
            ws.rollback_quietly(golden)
            ws.rollback_quietly(env)
            err = ws.as_write_error(e, what)
            if err is e:
                raise
            raise err from e


def delete_execution_strict(status_config: Any, account_executions_id: int) -> Dict[str, Any]:
    """Hard-delete one execution: its Golden Source raw row, its commission, its attribution.

    Returns ``{"deleted": "hard", "account_executions_id", "allocations_removed"}``.
    Refused (WriteConflict) while an option/stock link names it -- those links have no
    FK and would be left pointing at nothing (``delete_one_execution`` leaves them). The
    commission row (keyed by ``exec_id``) is removed only when no other raw row -- the
    same fill recorded by TWS and by Flex -- still carries that ``exec_id``. Needs the
    status config. The attribution (this env's trade_execution) goes too unless
    the twin still carries the (account_id, exec_id); ``allocations_removed`` counts the
    split rows removed. Raises WriteNotFound, WriteConflict, WriteFailed.
    """
    what = f"execution {account_executions_id}"
    if not isinstance(status_config, dict):
        raise WriteFailed(f"Cannot delete {what}: the status config is needed (it spans two databases).")
    raw_tbl, pk_col, pk_val = _raw_table_pk_for_account_executions_id(account_executions_id)
    eid = int(account_executions_id)
    with ws.write_connection(status_config, what) as env, ws.write_connection(status_config, what, golden=True) as golden:
        try:
            with golden.cursor() as gcur, env.cursor() as ecur:
                key = _fill_key(gcur, raw_tbl, pk_col, pk_val, lock=True)
                if key is None:
                    raise WriteNotFound(f"No execution {account_executions_id}.")
                exec_id = key[1] or None
                ecur.execute(
                    f"SELECT count(*) FROM {OPTION_STOCK_LINK} "
                    "WHERE option_account_executions_id = %s OR stock_account_executions_id = %s",
                    (eid, eid),
                )
                links = int((ecur.fetchone() or [0])[0] or 0)
                if links:
                    raise WriteConflict(
                        f"This execution is in {ws.plural(links, 'option/stock link', 'option/stock links')}; "
                        "unlink it first."
                    )
                gcur.execute(f"DELETE FROM {raw_tbl} WHERE {pk_col} = %s", (pk_val,))
                if gcur.rowcount == 0:
                    raise WriteNotFound(f"No execution {account_executions_id}.")
                allocations = _drop_attribution_if_last(gcur, ecur, key[0], key[1])
                if exec_id:
                    still_used = " AND ".join(
                        f"NOT EXISTS (SELECT 1 FROM {t} WHERE exec_id = %s)" for t in _GOLDEN_RAW_TABLES
                    )
                    gcur.execute(
                        f"DELETE FROM {GOLDEN_COMMISSIONS} WHERE exec_id = %s AND {still_used}",
                        [exec_id] * (1 + len(_GOLDEN_RAW_TABLES)),
                    )
            golden.commit()
            env.commit()
        except Exception as e:
            ws.rollback_quietly(golden)
            ws.rollback_quietly(env)
            err = ws.as_write_error(e, what, on_fk="conflict")
            if err is e:
                raise
            raise err from e
    return {"deleted": "hard", "account_executions_id": eid, "allocations_removed": allocations}


__all__ = [
    "sync_accounts_snapshot_to_db",
    "write_account_executions_to_db",
    "update_execution_commission",
    "insert_one_execution",
    "upsert_account_transactions",
    "update_one_execution",
    "delete_one_execution",
    "patch_execution",
    "delete_execution_strict",
]
