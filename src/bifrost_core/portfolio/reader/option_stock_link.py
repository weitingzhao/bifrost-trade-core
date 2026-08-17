"""Link option execution rows to underlying stock fills (account_execution_option_stock_link)."""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.brokerage_tables import EXECUTIONS_FINAL, OPTION_STOCK_LINK
from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)

_EXEC_FINAL = EXECUTIONS_FINAL
_LINK_TABLE = OPTION_STOCK_LINK

# Match portfolio reader executions._QTY_NORM_E (alias e) for consistent signed qty.
_QTY_NORM_E = (
    "CASE WHEN lower(trim(COALESCE(e.source, ''))) = 'tws_client' THEN e.quantity "
    "WHEN upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S') THEN -e.quantity "
    "ELSE e.quantity END"
)


def _normalized_signed_qty(source: Any, side: Any, quantity: Any) -> float:
    """Python mirror of flex/journal quantity normalization (non-tws_client: Sell → negative)."""
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


def underlying_symbol_from_row(row: Dict[str, Any]) -> str:
    """Underlying root for OPT rows: Flex underlying_symbol, else first token of local symbol."""
    u = (row.get("underlying_symbol") or "").strip()
    if u:
        return u.upper()
    sym = (row.get("symbol") or "").strip()
    if sym:
        return sym.split()[0].upper()
    return ""


def slippage_amount_vs_close(signed_qty: float, price: Any, close_price: Any) -> Optional[float]:
    """Cash slippage vs reference close: signed_qty * (price - close_price). None if inputs missing."""
    if price is None:
        return None
    try:
        p = float(price)
        cp = float(close_price) if close_price is not None else None
    except (TypeError, ValueError):
        return None
    if cp is None:
        return None
    if not isinstance(signed_qty, (int, float)):
        return None
    if signed_qty != signed_qty:  # NaN
        return None
    return float(signed_qty) * (p - cp)


def _connect(status_config: dict):
    params = _get_conn_params(status_config)
    return psycopg2.connect(**params)


def fetch_execution_final_row(
    conn: Any,
    account_id: str,
    account_executions_id: int,
) -> Optional[Dict[str, Any]]:
    """One row from account_executions_final by unified id."""
    acc = (account_id or "").strip()
    if not acc:
        return None
    try:
        eid = int(account_executions_id)
    except (TypeError, ValueError):
        return None
    sql = f"""
        SELECT account_executions_id, account_id, sec_type, symbol, underlying_symbol, multiplier,
               side, quantity, source, trade_date, exec_time, price, close_price
        FROM {_EXEC_FINAL} e
        WHERE e.account_id = %s AND e.account_executions_id = %s
        LIMIT 1
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (acc, eid))
            row = cur.fetchone()
        return dict(row) if row else None
    except Exception as ex:
        logger.debug("fetch_execution_final_row: %s", ex)
        return None


def _expected_stock_shares(option_row: Dict[str, Any]) -> Optional[float]:
    """Shares implied by option contracts × multiplier (Flex-style)."""
    try:
        mult = float(option_row.get("multiplier") or 0)
    except (TypeError, ValueError):
        mult = 0.0
    if mult <= 0:
        mult = 100.0
    qn = _normalized_signed_qty(
        option_row.get("source"),
        option_row.get("side"),
        option_row.get("quantity"),
    )
    contracts = abs(qn)
    if contracts <= 0:
        return None
    return contracts * mult


def insert_option_stock_link(
    status_config: dict,
    body: Dict[str, Any],
) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
    """
    Insert one link. Validates both legs exist on account_executions_final, OPT + STK, same account.
    Returns (ok, link_id, error_message, warning_message).
    """
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False, None, "PostgreSQL is required.", None
    account_id = (body.get("account_id") or "").strip()
    if not account_id:
        return False, None, "account_id is required.", None
    try:
        oid = int(body["option_account_executions_id"])
        sid = int(body["stock_account_executions_id"])
    except (KeyError, TypeError, ValueError):
        return False, None, "option_account_executions_id and stock_account_executions_id are required.", None
    role = body.get("role")
    if role is not None:
        role = str(role).strip().lower() or None
        if role and role not in ("exercise", "assignment"):
            return False, None, "role must be exercise, assignment, or omitted.", None
    note = body.get("note")
    if note is not None:
        note = str(note).strip() or None

    conn = _connect(status_config)
    try:
        opt = fetch_execution_final_row(conn, account_id, oid)
        stk = fetch_execution_final_row(conn, account_id, sid)
        if not opt:
            return False, None, "Option execution not found in performance book (Flex/journal).", None
        if not stk:
            return False, None, "Stock execution not found in performance book (Flex/journal).", None
        if (opt.get("account_id") or "").strip() != account_id or (stk.get("account_id") or "").strip() != account_id:
            return False, None, "account_id mismatch.", None
        st_o = (str(opt.get("sec_type") or "")).strip().upper()
        st_s = (str(stk.get("sec_type") or "")).strip().upper()
        if st_o != "OPT":
            return False, None, "option_account_executions_id must refer to an OPT row.", None
        if st_s != "STK":
            return False, None, "stock_account_executions_id must refer to an STK row.", None
        und_o = underlying_symbol_from_row(opt)
        sym_s = (stk.get("symbol") or "").strip().upper()
        if und_o and sym_s and und_o != sym_s:
            return (
                False,
                None,
                f"Underlying symbol ({und_o}) does not match stock symbol ({sym_s}).",
                None,
            )

        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {_LINK_TABLE} (
                    account_id, option_account_executions_id, stock_account_executions_id, role, note
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (option_account_executions_id, stock_account_executions_id) DO NOTHING
                RETURNING account_execution_option_stock_link_id
                """,
                (account_id, oid, sid, role, note),
            )
            ret = cur.fetchone()
            if not ret or ret[0] is None:
                conn.rollback()
                return False, None, "Link already exists or insert failed.", None
            link_id = int(ret[0])
            conn.commit()

        warning: Optional[str] = None
        exp = _expected_stock_shares(opt)
        if exp is not None and exp > 0:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT e.side, e.quantity, e.source
                    FROM {_LINK_TABLE} l
                    JOIN {_EXEC_FINAL} e
                      ON e.account_executions_id = l.stock_account_executions_id
                     AND e.account_id = l.account_id
                    WHERE l.account_id = %s AND l.option_account_executions_id = %s
                    """,
                    (account_id, oid),
                )
                rows = cur.fetchall() or []
            total_abs = 0.0
            for r in rows:
                total_abs += abs(
                    _normalized_signed_qty(r.get("source"), r.get("side"), r.get("quantity"))
                )
            if abs(total_abs - exp) > 1e-6:
                warning = (
                    f"Linked stock share count ({total_abs:g}) differs from "
                    f"option-implied shares ({exp:g})."
                )

        return True, link_id, None, warning
    except Exception as ex:
        logger.warning("insert_option_stock_link: %s", ex)
        try:
            conn.rollback()
        except Exception:
            pass
        return False, None, str(ex), None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def delete_option_stock_link(status_config: dict, link_id: int, account_id: str) -> Tuple[bool, Optional[str]]:
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False, "PostgreSQL is required."
    acc = (account_id or "").strip()
    if not acc:
        return False, "account_id is required."
    try:
        lid = int(link_id)
    except (TypeError, ValueError):
        return False, "Invalid link id."
    conn = _connect(status_config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {_LINK_TABLE} WHERE account_execution_option_stock_link_id = %s AND account_id = %s",
                (lid, acc),
            )
            deleted = cur.rowcount
            conn.commit()
        if deleted == 0:
            return False, "Link not found or wrong account."
        return True, None
    except Exception as ex:
        logger.warning("delete_option_stock_link: %s", ex)
        try:
            conn.rollback()
        except Exception:
            pass
        return False, str(ex)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_option_stock_links(
    conn: Any,
    account_id: str,
    option_account_executions_id: int,
) -> Dict[str, Any]:
    """List links with stock columns and slippage; includes slippage_total."""
    acc = (account_id or "").strip()
    if not conn or not acc:
        return {"links": [], "slippage_total": None}
    try:
        oid = int(option_account_executions_id)
    except (TypeError, ValueError):
        return {"links": [], "slippage_total": None}

    sql = f"""
        SELECT
            l.account_execution_option_stock_link_id AS link_id,
            l.option_account_executions_id,
            l.stock_account_executions_id,
            l.role,
            l.note,
            extract(epoch from l.created_at)::bigint AS created_at_epoch,
            e.symbol AS stock_symbol,
            e.side AS stock_side,
            {_QTY_NORM_E} AS stock_quantity,
            e.price AS stock_price,
            e.close_price AS stock_close_price,
            e.trade_date AS stock_trade_date,
            e.exec_id AS stock_exec_id
        FROM {_LINK_TABLE} l
        JOIN {_EXEC_FINAL} e
          ON e.account_executions_id = l.stock_account_executions_id
         AND e.account_id = l.account_id
        WHERE l.account_id = %s AND l.option_account_executions_id = %s
        ORDER BY e.trade_date ASC NULLS LAST, e.exec_time ASC NULLS LAST
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (acc, oid))
            rows = cur.fetchall() or []
    except Exception as ex:
        logger.debug("get_option_stock_links: %s", ex)
        return {"links": [], "slippage_total": None, "error": str(ex)}

    out: List[Dict[str, Any]] = []
    total = 0.0
    any_slip = False
    for r in rows:
        d = dict(r)
        sq = d.get("stock_quantity")
        try:
            sqf = float(sq) if sq is not None else 0.0
        except (TypeError, ValueError):
            sqf = 0.0
        slip = slippage_amount_vs_close(sqf, d.get("stock_price"), d.get("stock_close_price"))
        if slip is not None:
            any_slip = True
            total += slip
        d["slippage_vs_close"] = slip
        out.append(d)

    return {
        "links": out,
        "slippage_total": total if any_slip else None,
    }


def get_option_stock_links_bulk(
    conn: Any,
    batches: List[Tuple[str, List[int]]],
) -> Dict[str, Any]:
    """
    Merge (account_id, option ids) by account, then load link rows per account.
    Returns by_option_id: str(option_id) -> { links, slippage_total }.
    """
    out_by_option: Dict[int, Dict[str, Any]] = {}
    if not conn or not batches:
        return {"by_option_id": {}}

    merged: Dict[str, List[int]] = defaultdict(list)
    for account_id_raw, id_list in batches:
        acc = (account_id_raw or "").strip()
        if not acc or not id_list:
            continue
        for x in id_list:
            try:
                merged[acc].append(int(x))
            except (TypeError, ValueError):
                continue

    for acc, raw_ids in merged.items():
        uniq_ids = list(dict.fromkeys(raw_ids))[:4000]
        if not uniq_ids:
            continue

        sql = f"""
            SELECT
                l.account_execution_option_stock_link_id AS link_id,
                l.option_account_executions_id,
                l.stock_account_executions_id,
                l.role,
                l.note,
                extract(epoch from l.created_at)::bigint AS created_at_epoch,
                e.symbol AS stock_symbol,
                e.side AS stock_side,
                {_QTY_NORM_E} AS stock_quantity,
                e.price AS stock_price,
                e.close_price AS stock_close_price,
                e.trade_date AS stock_trade_date,
                e.exec_id AS stock_exec_id
            FROM {_LINK_TABLE} l
            JOIN {_EXEC_FINAL} e
              ON e.account_executions_id = l.stock_account_executions_id
             AND e.account_id = l.account_id
            WHERE l.account_id = %s AND l.option_account_executions_id = ANY(%s)
            ORDER BY l.option_account_executions_id, e.trade_date ASC NULLS LAST, e.exec_time ASC NULLS LAST
        """
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, (acc, uniq_ids))
                rows = cur.fetchall() or []
        except Exception as ex:
            logger.debug("get_option_stock_links_bulk: %s", ex)
            continue

        by_oid: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            d = dict(r)
            oid = d.get("option_account_executions_id")
            if oid is None:
                continue
            try:
                oid_i = int(oid)
            except (TypeError, ValueError):
                continue
            sq = d.get("stock_quantity")
            try:
                sqf = float(sq) if sq is not None else 0.0
            except (TypeError, ValueError):
                sqf = 0.0
            slip = slippage_amount_vs_close(sqf, d.get("stock_price"), d.get("stock_close_price"))
            d["slippage_vs_close"] = slip
            by_oid[oid_i].append(d)

        for oid_i, link_rows in by_oid.items():
            total = 0.0
            any_slip = False
            for d in link_rows:
                s = d.get("slippage_vs_close")
                if s is not None:
                    any_slip = True
                    total += float(s)
            out_by_option[oid_i] = {
                "links": link_rows,
                "slippage_total": total if any_slip else None,
            }

    json_out: Dict[str, Any] = {}
    for k, v in out_by_option.items():
        json_out[str(k)] = v
    return {"by_option_id": json_out}


def get_stock_link_candidates(
    conn: Any,
    account_id: str,
    option_account_executions_id: int,
    trade_date_from: Optional[str] = None,
    trade_date_to: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """
    STK rows in final book matching option underlying, date window, excluding already linked to this option.
    """
    acc = (account_id or "").strip()
    if not conn or not acc:
        return {"executions": [], "error": "account_id required"}
    try:
        oid = int(option_account_executions_id)
    except (TypeError, ValueError):
        return {"executions": [], "error": "invalid option_account_executions_id"}

    opt = fetch_execution_final_row(conn, acc, oid)
    if not opt:
        return {"executions": [], "error": "Option execution not found in performance book."}
    if (str(opt.get("sec_type") or "")).strip().upper() != "OPT":
        return {"executions": [], "error": "Not an OPT row."}

    und = underlying_symbol_from_row(opt)
    if not und:
        return {"executions": [], "error": "Could not resolve underlying symbol."}

    td = opt.get("trade_date")
    if hasattr(td, "isoformat"):
        center: date = td  # type: ignore[assignment]
    elif isinstance(td, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", td.strip()):
        y, m, d_ = td.strip().split("-")
        center = date(int(y), int(m), int(d_))
    else:
        center = date.today()

    if trade_date_from and re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date_from.strip()):
        d_from = date.fromisoformat(trade_date_from.strip()[:10])
    else:
        d_from = center - timedelta(days=7)
    if trade_date_to and re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date_to.strip()):
        d_to = date.fromisoformat(trade_date_to.strip()[:10])
    else:
        d_to = center + timedelta(days=7)

    lim = max(1, min(int(limit or 200), 500))

    sql = f"""
        SELECT
            e.account_executions_id,
            e.account_id,
            e.exec_id,
            extract(epoch from e.exec_time) AS time,
            e.symbol,
            e.sec_type,
            e.side,
            {_QTY_NORM_E} AS quantity,
            e.price,
            e.source,
            e.contract_key,
            e.trade_date,
            e.close_price,
            e.report_date,
            e.transaction_type
        FROM {_EXEC_FINAL} e
        WHERE e.account_id = %s
          AND upper(trim(COALESCE(e.sec_type, ''))) = 'STK'
          AND trim(upper(COALESCE(e.symbol, ''))) = %s
          AND (e.trade_date IS NULL OR (e.trade_date >= %s AND e.trade_date <= %s))
          AND NOT EXISTS (
            SELECT 1 FROM {_LINK_TABLE} l
            WHERE l.account_id = e.account_id
              AND l.option_account_executions_id = %s
              AND l.stock_account_executions_id = e.account_executions_id
          )
        ORDER BY e.trade_date DESC NULLS LAST, e.exec_time DESC NULLS LAST
        LIMIT %s
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, (acc, und, d_from, d_to, oid, lim))
            rows = cur.fetchall() or []
    except Exception as ex:
        logger.debug("get_stock_link_candidates: %s", ex)
        return {"executions": [], "error": str(ex)}

    execs: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        t = d.get("time")
        if t is not None:
            try:
                d["time"] = float(t)
            except (TypeError, ValueError):
                pass
        sq = d.get("quantity")
        try:
            sqf = float(sq) if sq is not None else 0.0
        except (TypeError, ValueError):
            sqf = 0.0
        slip = slippage_amount_vs_close(sqf, d.get("price"), d.get("close_price"))
        d["slippage_vs_close"] = slip
        execs.append(d)

    return {"executions": execs, "underlying_symbol": und, "trade_date_from": d_from.isoformat(), "trade_date_to": d_to.isoformat()}
