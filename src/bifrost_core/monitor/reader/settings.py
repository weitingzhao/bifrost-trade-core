"""Settings: IB config, Flex config. Conn-based and status_config-based APIs."""

import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)

_EXEC_READ_TABLE = "account_executions"

# ----- Conn-based (for common.StatusReader delegation) -----

def get_ib_config(conn: Any) -> Optional[Dict[str, Any]]:
    """Return settings row id=1: ib_host_account_id, flex ranges, stream account IDs.

    IB host/port/client IDs come from config YAML (see get_effective_ib_config), not from DB.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT ib_host_account_id, flex_default_range_days, flex_init_range_days, "
                "stream_host_account_id, stream_secondary_account_id FROM settings WHERE id = 1"
            )
            row = cur.fetchone()
        if row is None:
            return None
        out: Dict[str, Any] = {}
        if row.get("flex_default_range_days") is not None:
            try:
                out["flex_default_range_days"] = max(1, int(row["flex_default_range_days"]))
            except (TypeError, ValueError):
                out["flex_default_range_days"] = 30
        else:
            out["flex_default_range_days"] = 30
        if row.get("flex_init_range_days") is not None:
            try:
                out["flex_init_range_days"] = max(1, int(row["flex_init_range_days"]))
            except (TypeError, ValueError):
                out["flex_init_range_days"] = 360
        else:
            out["flex_init_range_days"] = 360
        if row.get("ib_host_account_id") is not None and str(row.get("ib_host_account_id")).strip():
            out["ib_host_account_id"] = str(row["ib_host_account_id"]).strip()
        else:
            out["ib_host_account_id"] = None
        if row.get("stream_host_account_id") is not None and str(row.get("stream_host_account_id")).strip():
            out["stream_host_account_id"] = str(row["stream_host_account_id"]).strip()
        else:
            out["stream_host_account_id"] = None
        if row.get("stream_secondary_account_id") is not None and str(row.get("stream_secondary_account_id")).strip():
            out["stream_secondary_account_id"] = str(row["stream_secondary_account_id"]).strip()
        else:
            out["stream_secondary_account_id"] = None
        return out
    except Exception as e:
        logger.debug("get_ib_config failed: %s", e)
        return None


def get_flex_config(conn: Any, purpose: Optional[str] = None) -> Any:
    """If purpose is None: return { host_token, secondary_token, rows }. If purpose is set: return list of { token, query_id, role, query_label, purpose }."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT ib_flex_host_token, ib_flex_secondary_token FROM settings WHERE id = 1"
            )
            settings_row = cur.fetchone()
        host_tok = (settings_row.get("ib_flex_host_token") or "").strip() if settings_row else ""
        sec_tok = (settings_row.get("ib_flex_secondary_token") or "").strip() if settings_row else ""
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if purpose is not None:
                cur.execute(
                    "SELECT query_host_id, query_secondary_id, query_label, purpose FROM settings_ib_flex WHERE purpose = %s ORDER BY sort_order, id",
                    (purpose,),
                )
            else:
                cur.execute(
                    "SELECT query_host_id, query_secondary_id, query_label, purpose FROM settings_ib_flex ORDER BY sort_order, id"
                )
            rows = cur.fetchall()
        if purpose is not None:
            out = []
            for r in rows:
                qh_raw = (r.get("query_host_id") or "").strip()
                qs_raw = (r.get("query_secondary_id") or "").strip()
                label = (r.get("query_label") or "").strip()
                purp = (r.get("purpose") or "").strip()
                qh_ids = [x.strip() for x in qh_raw.split(",") if x.strip()]
                qs_ids = [x.strip() for x in qs_raw.split(",") if x.strip()]
                if host_tok:
                    for qid in qh_ids:
                        out.append({"token": host_tok, "query_id": qid, "role": "host", "query_label": label or None, "purpose": purp or None})
                if sec_tok:
                    for qid in qs_ids:
                        out.append({"token": sec_tok, "query_id": qid, "role": "secondary", "query_label": label or None, "purpose": purp or None})
            return out
        out_rows = []
        for r in rows:
            item = {
                "query_host_id": (r.get("query_host_id") or "").strip(),
                "query_secondary_id": (r.get("query_secondary_id") or "").strip() or None,
            }
            if r.get("query_label") is not None and str(r.get("query_label")).strip():
                item["query_label"] = str(r["query_label"]).strip()
            if r.get("purpose") is not None and str(r.get("purpose")).strip():
                item["purpose"] = str(r["purpose"]).strip()
            out_rows.append(item)
        return {"host_token": host_tok or None, "secondary_token": sec_tok or None, "rows": out_rows}
    except Exception as e:
        logger.debug("get_flex_config failed: %s", e)
        return [] if purpose is not None else {"host_token": None, "secondary_token": None, "rows": []}


def get_flex_default_range_dates(conn: Any) -> Tuple[str, str]:
    """Return (from_date, to_date) in yyyyMMdd for Flex default range. Uses settings.flex_default_range_days; to_date = yesterday."""
    days = 30
    try:
        ib = get_ib_config(conn)
        if ib and ib.get("flex_default_range_days") is not None:
            days = max(1, int(ib["flex_default_range_days"]))
    except Exception:
        pass
    yesterday = date.today() - timedelta(days=1)
    start = yesterday - timedelta(days=days)
    return start.strftime("%Y%m%d"), yesterday.strftime("%Y%m%d")


def get_flex_executions_stats(conn: Any) -> Dict[str, Any]:
    """Return basic stats for executions imported from Flex (source='flex_trades')."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS count,
                    COUNT(DISTINCT account_id) AS accounts,
                    MIN(exec_time)::date AS min_date,
                    MAX(exec_time)::date AS max_date
                FROM {_EXEC_READ_TABLE}
                WHERE source = %s
                """,
                ("flex_trades",),
            )
            row = cur.fetchone() or {}
        return {
            "count": int(row.get("count") or 0),
            "accounts": int(row.get("accounts") or 0),
            "min_date": row.get("min_date"),
            "max_date": row.get("max_date"),
        }
    except Exception as e:
        logger.warning("get_flex_executions_stats failed: %s", e)
        return {"count": 0, "accounts": 0, "min_date": None, "max_date": None}


def get_flex_init_range_dates(conn: Any) -> Tuple[str, str]:
    """Return (from_date, to_date) in yyyyMMdd for Flex initial/full pull. Uses settings.flex_init_range_days; to_date = yesterday."""
    days = 360
    try:
        ib = get_ib_config(conn)
        if ib and ib.get("flex_init_range_days") is not None:
            days = max(1, int(ib["flex_init_range_days"]))
    except Exception:
        pass
    yesterday = date.today() - timedelta(days=1)
    start = yesterday - timedelta(days=days)
    return start.strftime("%Y%m%d"), yesterday.strftime("%Y%m%d")


# ----- Module-level (status_config) for re-export -----

def write_ib_config(
    status_config: dict,
    ib_host_account_id: Optional[str] = None,
    stream_host_account_id: Optional[str] = None,
    stream_secondary_account_id: Optional[str] = None,
) -> bool:
    """Update settings (id=1): ib_host_account_id, stream_*_account_id. IB host/port/client IDs are not stored in DB."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    host_val = (ib_host_account_id or "").strip() or None
    stream_host_val = (stream_host_account_id or "").strip() or None
    stream_secondary_val = (stream_secondary_account_id or "").strip() or None
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE settings SET
                        ib_host_account_id = %s,
                        stream_host_account_id = %s,
                        stream_secondary_account_id = %s
                    WHERE id = 1
                    """,
                    (host_val, stream_host_val, stream_secondary_val),
                )
                if cur.rowcount == 0:
                    cur.execute(
                        """
                        INSERT INTO settings (id, ib_host_account_id, stream_host_account_id, stream_secondary_account_id)
                        VALUES (1, %s, %s, %s)
                        """,
                        (host_val, stream_host_val, stream_secondary_val),
                    )
            conn.commit()
            logger.info("[R-A3] write_ib_config: wrote settings id=1 account/stream fields")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_ib_config failed: %s", e)
        return False


def write_flex_config(
    status_config: dict,
    host_token: Optional[str],
    secondary_token: Optional[str],
    accounts: List[Dict[str, Any]],
    flex_default_range_days: Optional[int] = None,
    flex_init_range_days: Optional[int] = None,
) -> bool:
    """Write Flex tokens to settings and replace settings_ib_flex with rows. Returns True on success."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                days_val = max(1, int(flex_default_range_days)) if flex_default_range_days is not None else None
                init_val = max(1, int(flex_init_range_days)) if flex_init_range_days is not None else None
                cur.execute(
                    "UPDATE settings SET ib_flex_host_token = %s, ib_flex_secondary_token = %s, "
                    "flex_default_range_days = COALESCE(%s, flex_default_range_days), flex_init_range_days = COALESCE(%s, flex_init_range_days) WHERE id = 1",
                    ((host_token or "").strip() or None, (secondary_token or "").strip() or None, days_val, init_val),
                )
                cur.execute("DELETE FROM settings_ib_flex")
                for i, a in enumerate(accounts):
                    if not isinstance(a, dict):
                        continue
                    qh = (a.get("query_host_id") or "").strip()
                    if not qh:
                        continue
                    qs = (a.get("query_secondary_id") or "").strip() or None
                    query_label = (a.get("query_label") or "").strip() or None
                    purpose = (a.get("purpose") or "cash_transactions").strip() or "cash_transactions"
                    cur.execute(
                        "INSERT INTO settings_ib_flex (sort_order, query_label, purpose, query_host_id, query_secondary_id) VALUES (%s, %s, %s, %s, %s)",
                        (i, query_label, purpose, qh, qs),
                    )
            conn.commit()
            logger.info("write_flex_config: wrote tokens to settings and %d Flex row(s)", len([x for x in accounts if isinstance(x, dict) and (x.get("query_host_id") or "").strip()]))
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_flex_config failed: %s", e)
        return False


def write_active_strategy_and_gates(
    status_config: dict,
    active_strategy_structure_id: Optional[int] = None,
    active_gate_safety_strategy_id: Optional[int] = None,
    active_strategy_allocation_id: Optional[int] = None,
) -> bool:
    """Update settings (id=1): active_strategy_structure_id, active_gate_safety_strategy_id, active_strategy_allocation_id. Returns True on success."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE settings SET
                        active_strategy_structure_id = %s,
                        active_gate_safety_strategy_id = %s,
                        active_strategy_allocation_id = %s
                    WHERE id = 1
                    """,
                    (active_strategy_structure_id, active_gate_safety_strategy_id, active_strategy_allocation_id),
                )
            conn.commit()
            logger.info(
                "write_active_strategy_and_gates: active_strategy_structure_id=%s active_gate_safety_strategy_id=%s active_strategy_allocation_id=%s",
                active_strategy_structure_id, active_gate_safety_strategy_id, active_strategy_allocation_id,
            )
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_active_strategy_and_gates failed: %s", e)
        return False


__all__ = [
    "write_ib_config",
    "write_flex_config",
    "write_active_strategy_and_gates",
]
