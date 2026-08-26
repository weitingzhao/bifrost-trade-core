"""Accounts: snapshot read/write and execution/transaction write.
Execution/transaction read and preference_position_categories live in executions and position_categories modules. All logic inlined from legacy; no dependency on _legacy."""

import json
import logging
import math
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.accounts_sync import sync_accounts_snapshot_to_tables
from bifrost_core.persistence.postgres.brokerage_tables import (
    ACCOUNT,
    COMMISSIONS,
    CONTRACT_QUOTE_LIVE,
    EXECUTIONS,
    EXECUTIONS_RAW_FLEX,
    EXECUTIONS_RAW_JOURNAL,
    EXECUTIONS_RAW_TWS,
    GOLDEN_COMMISSIONS,
    GOLDEN_EXECUTIONS_RAW_FLEX,
    GOLDEN_EXECUTIONS_RAW_JOURNAL,
    GOLDEN_EXECUTIONS_RAW_TWS,
    GOLDEN_TRANSACTIONS,
    INSTANCE_ALLOCATION,
    POSITIONS,
    TRANSACTIONS,
)
from bifrost_core.persistence.postgres.connection import (
    _get_conn_params,
    _get_golden_source_conn_params,
)

from bifrost_core.monitor.reader import market as market_module
from bifrost_core.portfolio.reader.accounts_helpers import (
    _exec_time_to_dt,
    _has_meaningful_commission,
    resolve_daily_prev_close_from_fallback,
    stk_contract_quote_stale_for_positions,
)

logger = logging.getLogger(__name__)

# Logical account_executions_id in unified views → physical raw table + PK (see persistence postgres ddl account_executions / account_executions_final).
_JOURNAL_ID_OFFSET = 1000000000
_EXEC_READ_TABLE = EXECUTIONS


def _normalized_signed_qty_from_raw(source: Any, side: Any, quantity: Any) -> float:
    """Match servers/reader/executions _QTY_NORM for flex/journal (not tws_raw scope)."""
    try:
        q = float(quantity)
    except (TypeError, ValueError):
        return 0.0
    src = (str(source or "")).strip().lower()
    sd = (str(side or "")).strip().upper()
    if src == "tws_client":
        return q
    if sd in ("SELL", "SLD", "S"):
        return -abs(q)
    return q


def _apply_instance_allocations_on_cursor(
    cur: Any,
    account_executions_id: int,
    raw_tbl: str,
    pk_col: str,
    pk_val: int,
    body_allocations: List[Dict[str, Any]],
    *,
    raw_cur: Any = None,
) -> bool:
    """DELETE + optional INSERT on per-env INSTANCE_ALLOCATION; clear strategy cols on raw (golden).

    ``cur`` is the per-env cursor (instance allocation + strategy_instance).
    ``raw_cur`` is the golden_source cursor for raw execution tables; defaults to ``cur``.
    """
    rcur = raw_cur if raw_cur is not None else cur
    rcur.execute(
        f"SELECT account_id, quantity, side, source FROM {raw_tbl} WHERE {pk_col} = %s",
        (pk_val,),
    )
    row = rcur.fetchone()
    if not row:
        return False
    acc_id = (row[0] or "").strip()
    expected = _normalized_signed_qty_from_raw(row[3], row[2], row[1])
    cur.execute(
        f"DELETE FROM {INSTANCE_ALLOCATION} WHERE account_executions_id = %s",
        (int(account_executions_id),),
    )
    if not body_allocations:
        return True
    total = 0.0
    inserts: List[Tuple[int, float]] = []
    for item in body_allocations:
        if not isinstance(item, dict):
            return False
        si_raw = item.get("strategy_instance_id")
        aq_raw = item.get("allocated_quantity")
        if si_raw is None or aq_raw is None:
            return False
        try:
            si_id = int(si_raw)
            aq = float(aq_raw)
        except (TypeError, ValueError):
            return False
        cur.execute(
            "SELECT account_id FROM strategy_instance WHERE strategy_instance_id = %s",
            (si_id,),
        )
        si_row = cur.fetchone()
        if not si_row or (si_row[0] or "").strip() != acc_id:
            return False
        inserts.append((si_id, aq))
        total += aq
    if len({x[0] for x in inserts}) != len(inserts):
        return False
    if abs(total - expected) > 1e-5 * max(1.0, abs(expected)):
        return False
    for si_id, aq in inserts:
        cur.execute(
            f"""
            INSERT INTO {INSTANCE_ALLOCATION} (
                account_id, account_executions_id, strategy_instance_id, allocated_quantity
            ) VALUES (%s, %s, %s, %s)
            """,
            (acc_id, int(account_executions_id), si_id, aq),
        )
    rcur.execute(
        f"UPDATE {raw_tbl} SET strategy_instance_id = NULL, strategy_opportunity_id = NULL WHERE {pk_col} = %s",
        (pk_val,),
    )
    return True


def replace_execution_instance_allocations(
    status_config: dict,
    account_executions_id: int,
    body_allocations: Any,
) -> bool:
    """Replace or clear account_execution_instance_allocation rows. body_allocations: None=skip, []=delete all, list=replace."""
    if body_allocations is None:
        return True
    if not isinstance(body_allocations, list):
        return False
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    raw_tbl, pk_col, pk_val = _raw_table_pk_for_account_executions_id(account_executions_id)
    try:
        params = _get_conn_params(status_config)
        gs_params = _get_golden_source_conn_params(status_config)
        conn = psycopg2.connect(**params)
        golden = psycopg2.connect(**{**gs_params, "connect_timeout": 10})
        try:
            with conn.cursor() as cur, golden.cursor() as gcur:
                if not _apply_instance_allocations_on_cursor(
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
        logger.warning("replace_execution_instance_allocations failed: %s", e)
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
                        ip.bid AS price_bid,
                        ip.ask AS price_ask,
                        ip.mid AS price_mid,
                        ip.last AS price_last,
                        ip.updated_at AS price_updated_at,
                        pct.category_id AS position_category_id,
                        pc.name AS position_category_name,
                        w.optionable AS watchlist_optionable
                    FROM {POSITIONS} ap
                    LEFT JOIN {CONTRACT_QUOTE_LIVE} ip
                        ON ap.contract_key = ip.contract_key
                    LEFT JOIN preference_position_category_tags pct
                        ON ap.account_id = pct.account_id AND ap.contract_key = pct.contract_key
                    LEFT JOIN preference_position_categories pc
                        ON pct.category_id = pc.id
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

                raw_mid = p.get("price_mid")
                raw_last = p.get("price_last")
                sec_typ = (p.get("sec_type") or "").strip().upper()

                def _live_mid_or_last() -> Optional[float]:
                    for candidate in (raw_mid, raw_last):
                        if candidate is None:
                            continue
                        try:
                            v = float(candidate)
                        except (TypeError, ValueError):
                            continue
                        if not math.isfinite(v) or v <= 0:
                            continue
                        return v
                    return None

                from_live = _live_mid_or_last()
                price_val: Optional[float] = None
                used_stock_day_price = False

                if sec_typ == "STK":
                    if light:
                        price_val = from_live
                    else:
                        stale = stk_contract_quote_stale_for_positions(p)
                        fb = market_module.get_stock_day_fallback_price(conn, p.get("symbol") or "")
                        if from_live is not None and not stale:
                            price_val = from_live
                        elif fb is not None:
                            price_val = fb[0]
                            used_stock_day_price = True
                            pos_dict["price_updated_at"] = fb[1]
                            # Latest bar today → prev_close is yesterday; otherwise bar close is yesterday.
                            dpc = resolve_daily_prev_close_from_fallback(fb[0], fb[1], fb[2])
                            if dpc is not None:
                                pos_dict["daily_prev_close"] = dpc
                        elif from_live is not None:
                            price_val = from_live
                else:
                    price_val = from_live

                if price_val is not None:
                    pos_dict["price"] = price_val

                raw_updated = next(
                    (p[k] for k in p if k and k.lower() == "price_updated_at"),
                    p.get("price_updated_at"),
                )
                if not used_stock_day_price and raw_updated is not None:
                    try:
                        if hasattr(raw_updated, "timestamp"):
                            pos_dict["price_updated_at"] = raw_updated.timestamp()
                        elif isinstance(raw_updated, (int, float)) and math.isfinite(float(raw_updated)):
                            pos_dict["price_updated_at"] = float(raw_updated)
                        elif isinstance(raw_updated, str) and raw_updated.strip():
                            s = raw_updated.strip()
                            parts = s.rsplit(" ", 1)
                            if len(parts) == 2 and len(parts[1]) == 5 and parts[1][0] in "+-" and parts[1][1:].isdigit():
                                dt_naive = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S.%f")
                                sign = -1 if parts[1][0] == "-" else 1
                                hours = sign * int(parts[1][1:3])
                                mins = sign * int(parts[1][3:5])
                                dt = dt_naive.replace(tzinfo=timezone(timedelta(hours=hours, minutes=mins)))
                                pos_dict["price_updated_at"] = dt.timestamp()
                            else:
                                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                                pos_dict["price_updated_at"] = dt.timestamp()
                    except (TypeError, ValueError, OSError):
                        pass

                price_for_pnl: Optional[float] = None
                if used_stock_day_price:
                    price_for_pnl = price_val
                else:
                    for candidate in (raw_last, raw_mid):
                        if candidate is None:
                            continue
                        try:
                            v = float(candidate)
                        except (TypeError, ValueError):
                            continue
                        if not math.isfinite(v) or v <= 0:
                            continue
                        price_for_pnl = v
                        break
                    if price_for_pnl is None and price_val is not None:
                        price_for_pnl = price_val
                pos_qty = p.get("position")
                pos_avg = p.get("avg_cost")
                sec_type = (p.get("sec_type") or "").strip().upper()
                if price_for_pnl is not None and pos_qty is not None and pos_avg is not None:
                    try:
                        q = float(pos_qty)
                        c = float(pos_avg)
                        if math.isfinite(q) and math.isfinite(c):
                            if sec_type == "OPT":
                                pos_dict["unrealized_pnl"] = round((price_for_pnl - c) * q * 100, 2)
                            else:
                                pos_dict["unrealized_pnl"] = round((price_for_pnl - c) * q, 2)
                    except (TypeError, ValueError):
                        pass

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
                                   u.strategy_instance_id,
                                   so.name AS strategy_opportunity_name,
                                   si.label AS strategy_instance_label
                            FROM (
                                SELECT DISTINCT contract_key, strategy_opportunity_id, strategy_instance_id
                                FROM (
                                    SELECT contract_key, strategy_opportunity_id, strategy_instance_id
                                    FROM {_exec_tbl}
                                    WHERE account_id = %s
                                      AND contract_key = ANY(%s::text[])
                                      AND (strategy_opportunity_id IS NOT NULL OR strategy_instance_id IS NOT NULL)
                                    UNION
                                    SELECT e.contract_key,
                                           si.strategy_opportunity_id,
                                           a.strategy_instance_id
                                    FROM account_execution_instance_allocation a
                                    INNER JOIN strategy_instance si ON a.strategy_instance_id = si.strategy_instance_id
                                    INNER JOIN {_exec_tbl} e
                                      ON e.account_executions_id = a.account_executions_id
                                     AND e.account_id IS NOT DISTINCT FROM a.account_id
                                    WHERE a.account_id = %s
                                      AND e.contract_key = ANY(%s::text[])
                                ) x
                            ) u
                            LEFT JOIN strategy_opportunity so ON u.strategy_opportunity_id = so.strategy_opportunity_id
                            LEFT JOIN strategy_instance si ON u.strategy_instance_id = si.strategy_instance_id
                            """,
                            (acc_id, ck_list, acc_id, ck_list),
                        )
                        for sl_row in cur_sl.fetchall():
                            ck = sl_row.get("contract_key") or ""
                            link: Dict[str, Any] = {}
                            if sl_row.get("strategy_opportunity_id") is not None:
                                link["strategy_opportunity_id"] = int(sl_row["strategy_opportunity_id"])
                            if sl_row.get("strategy_instance_id") is not None:
                                link["strategy_instance_id"] = int(sl_row["strategy_instance_id"])
                            if sl_row.get("strategy_opportunity_name"):
                                link["strategy_opportunity_name"] = str(sl_row["strategy_opportunity_name"]).strip()
                            if sl_row.get("strategy_instance_label"):
                                link["strategy_instance_label"] = str(sl_row["strategy_instance_label"]).strip()
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
        gs_params = _get_golden_source_conn_params(status_config)
        conn = psycopg2.connect(**{**gs_params, "connect_timeout": 10})
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
        gs_params = _get_golden_source_conn_params(status_config)
        conn = psycopg2.connect(**{**gs_params, "connect_timeout": 10})
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

                    # 对于期权，若来源为 TWS（tws_event / tws_client），在插入前按 localSymbol 规范重建 contract_key：
                    #   local_symbol = symbol + "  " + yymmdd + right + strike8
                    #   contract_key = local_symbol|sec_type|expiry|strike|option_right
                    sec_type_norm = (sec_type or "").strip().upper()
                    if sec_type_norm == "OPT":
                        source_norm = (source or "").strip()
                        if source_norm in ("tws_event", "tws_client"):
                            sym_key = (symbol or "").strip()
                            exp_val = expiry
                            if isinstance(exp_val, (int, float)) and math.isfinite(exp_val):
                                exp_key = str(int(exp_val))
                            else:
                                exp_key = (exp_val or "").strip().replace("-", "")
                            strike_raw = strike
                            try:
                                strike_key = float(strike_raw) if strike_raw not in ("", None) else None
                            except (TypeError, ValueError):
                                strike_key = None
                            right_key = (option_right or "").strip().upper()
                            if len(right_key) > 1:
                                right_key = "C" if right_key.startswith("C") else "P" if right_key.startswith("P") else right_key[:1]
                            if sym_key and exp_key and strike_key is not None and right_key:
                                exp_digits = "".join(ch for ch in exp_key if ch.isdigit())
                                yymmdd = exp_digits[2:8] if len(exp_digits) >= 8 else exp_digits[-6:]
                                try:
                                    strike_int = int(round(strike_key * 1000.0))
                                except (TypeError, ValueError, OverflowError):
                                    strike_int = None
                                if yymmdd and strike_int is not None:
                                    strike_8 = f"{strike_int:08d}"
                                    local_symbol = f"{sym_key}  {yymmdd}{right_key}{strike_8}"
                                    contract_key = "|".join(
                                        [
                                            local_symbol,
                                            "OPT",
                                            exp_key,
                                            str(strike_key),
                                            right_key,
                                        ]
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
                    # 传参时把 0 当作 NULL，避免 SQL 端用 0 覆盖已有值（含类型为 str 的 "0"）
                    def _null_if_zero(v):
                        if v is None:
                            return None
                        try:
                            if float(v) == 0:
                                return None
                        except (TypeError, ValueError):
                            pass
                        return v if (v != "" and v is not None) else None
                    commission_val = _null_if_zero(commission)
                    realized_pnl_val = _null_if_zero(realized_pnl)
                    yield_val = _null_if_zero(yield_)
                    yield_redemption_date_val = _null_if_zero(yield_redemption_date)
                    currency_val = currency if (currency and str(currency).strip()) else None
                    # 仅当有至少一个「有意义」的 commission 字段时才写 commission 表，避免 7 天拉取时用空数据覆盖 1 天拉到的有效值
                    has_comm = (
                        _has_meaningful_commission(commission)
                        or _has_meaningful_commission(realized_pnl)
                        or _has_meaningful_commission(currency, is_numeric=False)
                        or _has_meaningful_commission(yield_)
                        or _has_meaningful_commission(yield_redemption_date)
                    )
                    if exec_id and has_comm:
                        cur.execute(
                            f"""
                            INSERT INTO {GOLDEN_COMMISSIONS} (exec_id, commission, currency, realized_pnl, yield_, yield_redemption_date)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (exec_id) DO UPDATE SET
                                commission = CASE
                                    WHEN EXCLUDED.commission IS NOT NULL AND EXCLUDED.commission != 0 THEN EXCLUDED.commission
                                    ELSE {GOLDEN_COMMISSIONS}.commission
                                END,
                                currency = CASE
                                    WHEN EXCLUDED.currency IS NOT NULL AND TRIM(COALESCE(EXCLUDED.currency, '')) != '' THEN EXCLUDED.currency
                                    ELSE {GOLDEN_COMMISSIONS}.currency
                                END,
                                realized_pnl = CASE
                                    WHEN EXCLUDED.realized_pnl IS NOT NULL AND EXCLUDED.realized_pnl != 0 THEN EXCLUDED.realized_pnl
                                    ELSE {GOLDEN_COMMISSIONS}.realized_pnl
                                END,
                                yield_ = CASE
                                    WHEN EXCLUDED.yield_ IS NOT NULL AND EXCLUDED.yield_ != 0 THEN EXCLUDED.yield_
                                    ELSE {GOLDEN_COMMISSIONS}.yield_
                                END,
                                yield_redemption_date = CASE
                                    WHEN EXCLUDED.yield_redemption_date IS NOT NULL AND EXCLUDED.yield_redemption_date != 0 THEN EXCLUDED.yield_redemption_date
                                    ELSE {GOLDEN_COMMISSIONS}.yield_redemption_date
                                END
                            """,
                            (exec_id, commission_val, currency_val, realized_pnl_val, yield_val, yield_redemption_date_val),
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
    """R-A2: 收到 IB commissionReport 事件时按 exec_id 写入 account_execution_commissions。"""
    if not exec_id or not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    def _nz(v):
        if v is None:
            return None
        try:
            if float(v) == 0:
                return None
        except (TypeError, ValueError):
            pass
        return v
    commission_val = _nz(commission)
    realized_pnl_val = _nz(realized_pnl)
    yield_val = _nz(yield_)
    yield_redemption_date_val = _nz(yield_redemption_date)
    currency_val = currency if (currency and str(currency).strip()) else None
    try:
        gs_params = _get_golden_source_conn_params(status_config)
        conn = psycopg2.connect(**{**gs_params, "connect_timeout": 10})
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {GOLDEN_COMMISSIONS} (exec_id, commission, currency, realized_pnl, yield_, yield_redemption_date)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (exec_id) DO UPDATE SET
                        commission = CASE
                            WHEN EXCLUDED.commission IS NOT NULL AND EXCLUDED.commission != 0 THEN EXCLUDED.commission
                            ELSE {GOLDEN_COMMISSIONS}.commission
                        END,
                        currency = CASE
                            WHEN EXCLUDED.currency IS NOT NULL AND TRIM(COALESCE(EXCLUDED.currency, '')) != '' THEN EXCLUDED.currency
                            ELSE {GOLDEN_COMMISSIONS}.currency
                        END,
                        realized_pnl = CASE
                            WHEN EXCLUDED.realized_pnl IS NOT NULL AND EXCLUDED.realized_pnl != 0 THEN EXCLUDED.realized_pnl
                            ELSE {GOLDEN_COMMISSIONS}.realized_pnl
                        END,
                        yield_ = CASE
                            WHEN EXCLUDED.yield_ IS NOT NULL AND EXCLUDED.yield_ != 0 THEN EXCLUDED.yield_
                            ELSE {GOLDEN_COMMISSIONS}.yield_
                        END,
                        yield_redemption_date = CASE
                            WHEN EXCLUDED.yield_redemption_date IS NOT NULL AND EXCLUDED.yield_redemption_date != 0 THEN EXCLUDED.yield_redemption_date
                            ELSE {GOLDEN_COMMISSIONS}.yield_redemption_date
                        END
                    """,
                    (exec_id, commission_val, currency_val, realized_pnl_val, yield_val, yield_redemption_date_val),
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
    strategy_opportunity_id = body.get("strategy_opportunity_id")
    strategy_instance_id = body.get("strategy_instance_id")
    if strategy_opportunity_id is not None:
        try:
            strategy_opportunity_id = int(strategy_opportunity_id)
        except (TypeError, ValueError):
            strategy_opportunity_id = None
    if strategy_instance_id is not None:
        try:
            strategy_instance_id = int(strategy_instance_id)
        except (TypeError, ValueError):
            strategy_instance_id = None
    exec_dt = _exec_time_to_dt(exec_time)
    try:
        params = _get_conn_params(status_config)
        gs_params = _get_golden_source_conn_params(status_config)
        env_conn = psycopg2.connect(**params)
        golden = psycopg2.connect(**{**gs_params, "connect_timeout": 10})
        try:
            with golden.cursor() as cur, env_conn.cursor() as env_cur:
                cols = "account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, raw_extra, strategy_opportunity_id, strategy_instance_id"
                placeholders = "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
                vals = (account_id, exec_id, exec_dt, symbol, sec_type, side, quantity, price, source, expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, raw_extra, strategy_opportunity_id, strategy_instance_id)
                new_id = None
                # Write physical raw tables on golden_source (executions view is read-only).
                if source == "journal_closed":
                    cur.execute(
                        f"""
                        INSERT INTO {GOLDEN_EXECUTIONS_RAW_JOURNAL} ({cols}, legacy_account_executions_id)
                        VALUES ({placeholders}, NULL)
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
                        INSERT INTO {GOLDEN_EXECUTIONS_RAW_TWS} ({cols}, legacy_account_executions_id)
                        VALUES ({placeholders}, NULL)
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
                    cur.execute(
                        f"""
                        INSERT INTO {GOLDEN_COMMISSIONS} (exec_id, commission, currency, realized_pnl, yield_, yield_redemption_date)
                        VALUES (%s, %s, %s, %s, NULL, NULL)
                        ON CONFLICT (exec_id) DO UPDATE SET
                            commission = COALESCE(EXCLUDED.commission, {GOLDEN_COMMISSIONS}.commission),
                            currency = COALESCE(NULLIF(TRIM(COALESCE(EXCLUDED.currency, '')), ''), {GOLDEN_COMMISSIONS}.currency),
                            realized_pnl = COALESCE(EXCLUDED.realized_pnl, {GOLDEN_COMMISSIONS}.realized_pnl)
                        """,
                        (exec_id, commission, currency or None, realized_pnl),
                    )
                if new_id is not None and body.get("instance_allocations") is not None:
                    ia = body.get("instance_allocations")
                    if not isinstance(ia, list):
                        env_conn.rollback()
                        golden.rollback()
                        return None
                    rtbl, rpkc, rpkv = _raw_table_pk_for_account_executions_id(new_id)
                    if not _apply_instance_allocations_on_cursor(
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



def upsert_account_transactions(status_config: dict, rows: List[Dict[str, Any]]) -> int:
    """Insert or update account_transactions from Flex cash transaction list. Returns number of rows processed.
    Each row at minimum: account_id, ts (Unix float), amount, type, currency?, description?.
    Extended fields (when present): flex_transaction_id, flex_type, flex_code, asset_category, asset_subcategory,
    symbol, conid, security_id, security_id_type, listing_exchange, report_date, available_for_trading_date,
    fx_rate_to_base, raw_extra.
    Uses ON CONFLICT (account_id, ts, amount, type) DO UPDATE to avoid duplicates."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return 0
    if not rows:
        return 0
    try:
        gs_params = _get_golden_source_conn_params(status_config)
        conn = psycopg2.connect(**{**gs_params, "connect_timeout": 10})
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
                        continue
                    if ts is None:
                        continue
                    try:
                        ts_float = float(ts)
                    except (TypeError, ValueError):
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
                        continue
                    available_for_trading_date = (r.get("available_for_trading_date") or "").strip() or None
                    fx_rate_to_base = r.get("fx_rate_to_base")
                    try:
                        fx_rate_to_base_float = float(fx_rate_to_base) if fx_rate_to_base is not None else None
                    except (TypeError, ValueError):
                        fx_rate_to_base_float = None
                    raw_extra = r.get("raw_extra")

                    cur.execute(
                        f"""
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
                        ON CONFLICT (account_id, ts, amount, type, report_date) DO UPDATE SET
                            currency = COALESCE(EXCLUDED.currency, {GOLDEN_TRANSACTIONS}.currency),
                            description = COALESCE(EXCLUDED.description, {GOLDEN_TRANSACTIONS}.description),
                            flex_transaction_id = COALESCE(EXCLUDED.flex_transaction_id, {GOLDEN_TRANSACTIONS}.flex_transaction_id),
                            flex_type = COALESCE(EXCLUDED.flex_type, {GOLDEN_TRANSACTIONS}.flex_type),
                            flex_code = COALESCE(EXCLUDED.flex_code, {GOLDEN_TRANSACTIONS}.flex_code),
                            asset_category = COALESCE(EXCLUDED.asset_category, {GOLDEN_TRANSACTIONS}.asset_category),
                            asset_subcategory = COALESCE(EXCLUDED.asset_subcategory, {GOLDEN_TRANSACTIONS}.asset_subcategory),
                            symbol = COALESCE(EXCLUDED.symbol, {GOLDEN_TRANSACTIONS}.symbol),
                            conid = COALESCE(EXCLUDED.conid, {GOLDEN_TRANSACTIONS}.conid),
                            security_id = COALESCE(EXCLUDED.security_id, {GOLDEN_TRANSACTIONS}.security_id),
                            security_id_type = COALESCE(EXCLUDED.security_id_type, {GOLDEN_TRANSACTIONS}.security_id_type),
                            listing_exchange = COALESCE(EXCLUDED.listing_exchange, {GOLDEN_TRANSACTIONS}.listing_exchange),
                            available_for_trading_date = COALESCE(EXCLUDED.available_for_trading_date, {GOLDEN_TRANSACTIONS}.available_for_trading_date),
                            fx_rate_to_base = COALESCE(EXCLUDED.fx_rate_to_base, {GOLDEN_TRANSACTIONS}.fx_rate_to_base),
                            raw_extra = COALESCE(EXCLUDED.raw_extra, {GOLDEN_TRANSACTIONS}.raw_extra)
                        """,
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
            conn.commit()
            return len(rows)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("upsert_account_transactions failed: %s", e)
        return 0


def update_one_execution(status_config: dict, account_executions_id: int, body: Dict[str, Any]) -> bool:
    """R-A2 扩展：按 account_executions_id 更新一条执行记录（手动修正）。写入物理表 executions_raw_flex / executions_raw_tws / executions_raw_journal（与 account_executions 视图编码一致）；不可 UPDATE 联合视图本身。body 可含任意子集：time, symbol, … strategy_opportunity_id, strategy_instance_id；以及 commission, realized_pnl, currency（写 account_execution_commissions）。"""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    # 可更新列（与 raw 表一致）
    exec_cols = ("exec_time", "symbol", "sec_type", "side", "quantity", "price", "account_id", "source", "expiry", "strike", "option_right", "exchange", "order_id", "cum_qty", "contract_key", "strategy_opportunity_id", "strategy_instance_id")
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
        if k in ("strategy_opportunity_id", "strategy_instance_id") and v is not None:
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = None
        if k == "raw_extra" and v is not None and not isinstance(v, str):
            v = json.dumps(v) if v else None
        updates.append(f'"{k}" = %s')
        values.append(v)
    raw_tbl, pk_col, pk_val = _raw_table_pk_for_account_executions_id(account_executions_id)
    values.append(pk_val)
    try:
        params = _get_conn_params(status_config)
        gs_params = _get_golden_source_conn_params(status_config)
        env_conn = psycopg2.connect(**params)
        golden = psycopg2.connect(**{**gs_params, "connect_timeout": 10})
        try:
            with golden.cursor() as cur, env_conn.cursor() as env_cur:
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
                elif not any(k in body for k in commission_keys) and "instance_allocations" not in body:
                    return False
                if "instance_allocations" in body:
                    ia = body.get("instance_allocations")
                    if ia is not None and not isinstance(ia, list):
                        golden.rollback()
                        env_conn.rollback()
                        return False
                    if ia is not None:
                        if not _apply_instance_allocations_on_cursor(
                            env_cur, account_executions_id, raw_tbl, pk_col, pk_val, ia, raw_cur=cur
                        ):
                            golden.rollback()
                            env_conn.rollback()
                            return False
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
                    comm = body.get("commission")
                    pnl = body.get("realized_pnl")
                    cur_ = body.get("currency")
                    cur.execute(
                        f"""
                        INSERT INTO {GOLDEN_COMMISSIONS} (exec_id, commission, currency, realized_pnl, yield_, yield_redemption_date)
                        VALUES (%s, %s, %s, %s, NULL, NULL)
                        ON CONFLICT (exec_id) DO UPDATE SET
                            commission = CASE WHEN EXCLUDED.commission IS NOT NULL THEN EXCLUDED.commission ELSE {GOLDEN_COMMISSIONS}.commission END,
                            currency = CASE WHEN EXCLUDED.currency IS NOT NULL AND TRIM(EXCLUDED.currency) != '' THEN EXCLUDED.currency ELSE {GOLDEN_COMMISSIONS}.currency END,
                            realized_pnl = CASE WHEN EXCLUDED.realized_pnl IS NOT NULL THEN EXCLUDED.realized_pnl ELSE {GOLDEN_COMMISSIONS}.realized_pnl END
                        """,
                        (exec_id, comm, cur_, pnl),
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
    """R-A2 扩展：按 account_executions_id 删除一条执行记录。删除 golden raw 行 + commissions；清理 per-env instance_allocation。"""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    raw_tbl, pk_col, pk_val = _raw_table_pk_for_account_executions_id(account_executions_id)
    try:
        params = _get_conn_params(status_config)
        gs_params = _get_golden_source_conn_params(status_config)
        env_conn = psycopg2.connect(**params)
        golden = psycopg2.connect(**{**gs_params, "connect_timeout": 10})
        try:
            with golden.cursor() as cur, env_conn.cursor() as env_cur:
                cur.execute(f"SELECT exec_id FROM {raw_tbl} WHERE {pk_col} = %s", (pk_val,))
                row = cur.fetchone()
                exec_id = row[0] if row and row[0] and str(row[0]).strip() else None
                env_cur.execute(
                    f"DELETE FROM {INSTANCE_ALLOCATION} WHERE account_executions_id = %s",
                    (int(account_executions_id),),
                )
                if exec_id:
                    cur.execute(f"DELETE FROM {GOLDEN_COMMISSIONS} WHERE exec_id = %s", (exec_id,))
                cur.execute(f"DELETE FROM {raw_tbl} WHERE {pk_col} = %s", (pk_val,))
                if cur.rowcount == 0:
                    golden.rollback()
                    env_conn.rollback()
                    return False
            golden.commit()
            env_conn.commit()
            return True
        finally:
            env_conn.close()
            golden.close()
    except Exception as e:
        logger.warning("delete_one_execution failed: account_executions_id=%s %s", account_executions_id, e)
        return False



def batch_update_execution_strategy(
    conn: Any,
    account_id: str,
    contract_key: Optional[str],
    execution_ids: Optional[List[int]],
    strategy_opportunity_id: Optional[int],
    strategy_instance_id: Optional[int],
) -> int:
    """Batch update strategy attribution on raw execution tables (not the account_executions union view).
    Either by contract_key (all matching rows in each raw table) or by explicit account_executions_id list."""
    if not conn or not (account_id or "").strip():
        return 0
    acc = str(account_id).strip()
    count = 0
    try:
        with conn.cursor() as cur:
            if execution_ids:
                cur.execute(
                    f"SELECT 1 FROM {INSTANCE_ALLOCATION} WHERE account_executions_id = ANY(%s) LIMIT 1",
                    (execution_ids,),
                )
                if cur.fetchone():
                    conn.rollback()
                    return -1
                for eid in execution_ids:
                    try:
                        raw_tbl, pk_col, pk_val = _raw_table_pk_for_account_executions_id(
                            int(eid), golden=False
                        )
                    except (TypeError, ValueError):
                        continue
                    cur.execute(
                        f"""
                        UPDATE {raw_tbl}
                        SET strategy_opportunity_id = %s, strategy_instance_id = %s
                        WHERE account_id = %s AND {pk_col} = %s
                        """,
                        (strategy_opportunity_id, strategy_instance_id, acc, pk_val),
                    )
                    count += cur.rowcount
            elif contract_key and contract_key.strip():
                ck = contract_key.strip()
                cur.execute(
                    f"""
                    SELECT 1 FROM {INSTANCE_ALLOCATION} a
                    WHERE a.account_id = %s
                      AND EXISTS (
                        SELECT 1 FROM {EXECUTIONS} e
                        WHERE e.account_executions_id = a.account_executions_id
                          AND e.account_id IS NOT DISTINCT FROM a.account_id
                          AND trim(COALESCE(e.contract_key, '')) = trim(COALESCE(%s, ''))
                      )
                    LIMIT 1
                    """,
                    (acc, ck),
                )
                if cur.fetchone():
                    conn.rollback()
                    return -1
                for raw_tbl in (EXECUTIONS_RAW_TWS, EXECUTIONS_RAW_FLEX, EXECUTIONS_RAW_JOURNAL):
                    cur.execute(
                        f"""
                        UPDATE {raw_tbl}
                        SET strategy_opportunity_id = %s, strategy_instance_id = %s
                        WHERE account_id = %s AND contract_key = %s
                        """,
                        (strategy_opportunity_id, strategy_instance_id, acc, ck),
                    )
                    count += cur.rowcount
            else:
                return 0
        conn.commit()
        return count
    except Exception as e:
        logger.warning("batch_update_execution_strategy failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return 0


__all__ = [
    "sync_accounts_snapshot_to_db",
    "write_account_executions_to_db",
    "update_execution_commission",
    "insert_one_execution",
    "upsert_account_transactions",
    "update_one_execution",
    "delete_one_execution",
    "batch_update_execution_strategy",
]
