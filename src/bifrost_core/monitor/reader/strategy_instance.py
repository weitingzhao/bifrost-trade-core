"""Strategy instance CRUD: list, get, create, update, open-legs. Used for trade attribution (SI.2)."""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

_EXEC_READ_TABLE = "account_executions_final"
_ALLOC_TABLE = "account_execution_instance_allocation"


def list_instances(
    conn: Any,
    account_id: Optional[str] = None,
    strategy_opportunity_id: Optional[int] = None,
    strategy_instance_ids: Optional[List[int]] = None,
    opened_at_from: Optional[float] = None,
    opened_at_until: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """List strategy instances, optionally filtered by account_id, strategy_opportunity_id, strategy_instance_ids, opened_at range (Unix seconds)."""
    if conn is None:
        return []
    try:
        conditions = []
        values: List[Any] = []
        if account_id is not None and str(account_id).strip():
            conditions.append("si.account_id = %s")
            values.append(str(account_id).strip())
        if strategy_opportunity_id is not None:
            conditions.append("si.strategy_opportunity_id = %s")
            values.append(strategy_opportunity_id)
        if strategy_instance_ids:
            placeholders = ", ".join(["%s"] * len(strategy_instance_ids))
            conditions.append(f"si.strategy_instance_id IN ({placeholders})")
            values.extend(strategy_instance_ids)
        if opened_at_from is not None and opened_at_from > 0:
            conditions.append("si.opened_at >= to_timestamp(%s)")
            values.append(opened_at_from)
        if opened_at_until is not None and opened_at_until > 0:
            conditions.append("si.opened_at <= to_timestamp(%s)")
            values.append(opened_at_until)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT si.strategy_instance_id, si.strategy_opportunity_id, si.account_id,
                       si.opened_at, si.label, si.notes, si.created_at, si.updated_at,
                       so.name AS strategy_opportunity_name,
                       ss.strategy_structure_id, ss.name AS strategy_structure_name,
                       (
                           SELECT COUNT(DISTINCT e.account_executions_id)
                           FROM {_EXEC_READ_TABLE} e
                           WHERE e.strategy_instance_id = si.strategy_instance_id
                              OR EXISTS (
                                  SELECT 1 FROM {_ALLOC_TABLE} a
                                  WHERE a.account_executions_id = e.account_executions_id
                                    AND a.strategy_instance_id = si.strategy_instance_id
                              )
                       ) AS executions_count
                FROM strategy_instance si
                LEFT JOIN strategy_opportunity so ON si.strategy_opportunity_id = so.strategy_opportunity_id
                LEFT JOIN strategy_structure ss ON so.strategy_structure_id = ss.strategy_structure_id
                {where}
                ORDER BY si.opened_at DESC
                """,
                values,
            )
            rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d.get("opened_at") is not None and hasattr(d["opened_at"], "timestamp"):
                d["opened_at_epoch"] = d["opened_at"].timestamp()
            if d.get("created_at") is not None and hasattr(d["created_at"], "timestamp"):
                d["created_at_epoch"] = d["created_at"].timestamp()
            if d.get("executions_count") is not None:
                d["executions_count"] = int(d["executions_count"])
            out.append(d)
        return out
    except Exception as e:
        logger.debug("list_instances failed: %s", e)
        return []


def get_instance_by_id(conn: Any, strategy_instance_id: int) -> Optional[Dict[str, Any]]:
    """Return one strategy instance by id, or None."""
    if conn is None:
        return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT si.strategy_instance_id, si.strategy_opportunity_id, si.account_id,
                       si.opened_at, si.label, si.notes, si.created_at, si.updated_at,
                       so.name AS strategy_opportunity_name,
                       ss.strategy_structure_id, ss.name AS strategy_structure_name
                FROM strategy_instance si
                LEFT JOIN strategy_opportunity so ON si.strategy_opportunity_id = so.strategy_opportunity_id
                LEFT JOIN strategy_structure ss ON so.strategy_structure_id = ss.strategy_structure_id
                WHERE si.strategy_instance_id = %s
                """,
                (strategy_instance_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        d = dict(row)
        if d.get("opened_at") is not None and hasattr(d["opened_at"], "timestamp"):
            d["opened_at_epoch"] = d["opened_at"].timestamp()
        if d.get("created_at") is not None and hasattr(d["created_at"], "timestamp"):
            d["created_at_epoch"] = d["created_at"].timestamp()
        return d
    except Exception as e:
        logger.debug("get_instance_by_id failed: %s", e)
        return None


def create_instance(
    conn: Any,
    strategy_opportunity_id: int,
    account_id: str,
    opened_at: Any,
    label: Optional[str] = None,
    notes: Optional[str] = None,
) -> Optional[int]:
    """Insert one strategy_instance. opened_at: datetime or Unix timestamp. Returns strategy_instance_id or None."""
    if conn is None:
        return None
    account_id = (account_id or "").strip()
    if not account_id:
        return None
    if isinstance(opened_at, (int, float)):
        try:
            opened_dt = datetime.fromtimestamp(float(opened_at), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    elif hasattr(opened_at, "timestamp"):
        opened_dt = opened_at
    else:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_instance (strategy_opportunity_id, account_id, opened_at, label, notes, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                RETURNING strategy_instance_id
                """,
                (strategy_opportunity_id, account_id, opened_dt, label or None, notes or None),
            )
            row = cur.fetchone()
        conn.commit()
        return int(row[0]) if row and row[0] is not None else None
    except Exception as e:
        logger.warning("create_instance failed: %s", e)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return None


def delete_instance(conn: Any, strategy_instance_id: int) -> bool:
    """Delete a strategy_instance by id. Returns True if deleted, False if not found or has linked executions."""
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM strategy_instance WHERE strategy_instance_id = %s",
                (strategy_instance_id,),
            )
            deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    except Exception as e:
        logger.warning("delete_instance failed: %s", e)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False


def update_instance(
    conn: Any,
    strategy_instance_id: int,
    label: Optional[str] = None,
    notes: Optional[str] = None,
    created_at: Optional[Any] = None,
    opened_at: Optional[Any] = None,
) -> bool:
    """Update label, notes, created_at, and/or opened_at of a strategy instance. created_at/opened_at: datetime or Unix timestamp. Returns True if a row was updated."""
    if conn is None:
        return False
    updates = []
    values: List[Any] = []
    if label is not None:
        updates.append("label = %s")
        values.append(label.strip() if isinstance(label, str) else label)
    if notes is not None:
        updates.append("notes = %s")
        values.append(notes.strip() if isinstance(notes, str) else notes)
    if created_at is not None:
        if isinstance(created_at, (int, float)):
            try:
                created_dt = datetime.fromtimestamp(float(created_at), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                created_dt = None
            if created_dt is not None:
                updates.append("created_at = %s")
                values.append(created_dt)
        elif hasattr(created_at, "timestamp"):
            updates.append("created_at = %s")
            values.append(created_at)
    if opened_at is not None:
        opened_dt = None
        if isinstance(opened_at, (int, float)):
            try:
                opened_dt = datetime.fromtimestamp(float(opened_at), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                pass
        elif hasattr(opened_at, "timestamp"):
            opened_dt = opened_at
        if opened_dt is not None:
            updates.append("opened_at = %s")
            values.append(opened_dt)
    if not updates:
        return True
    updates.append("updated_at = now()")
    values.append(strategy_instance_id)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE strategy_instance SET {', '.join(updates)} WHERE strategy_instance_id = %s",
                values,
            )
            if cur.rowcount == 0:
                return False
        conn.commit()
        return True
    except Exception as e:
        logger.warning("update_instance failed: %s", e)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False


def get_instance_open_option_legs(conn: Any, strategy_instance_id: int) -> List[Dict[str, Any]]:
    """Return current open OPT positions that have executions linked to this instance.
    Intersects account_executions (instance tagged) with account_positions (position != 0)."""
    if conn is None:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT ap.account_id, ap.contract_key, ap.symbol, ap.sec_type,
                       ap.position, ap.avg_cost, ap.expiry, ap.strike, ap.option_right,
                       ip.mid AS price_mid, ip.last AS price_last, ip.updated_at AS price_updated_at
                FROM account_positions ap
                INNER JOIN (
                    SELECT DISTINCT account_id, contract_key
                    FROM {_EXEC_READ_TABLE}
                    WHERE strategy_instance_id = %s
                      AND upper(trim(COALESCE(sec_type, ''))) = 'OPT'
                ) tagged ON ap.account_id = tagged.account_id AND ap.contract_key = tagged.contract_key
                LEFT JOIN contract_quote_live ip ON ap.contract_key = ip.contract_key
                WHERE ap.position IS NOT NULL AND ap.position != 0
                ORDER BY ap.contract_key
                """,
                (strategy_instance_id,),
            )
            rows = cur.fetchall()
        result: List[Dict[str, Any]] = []
        for r in rows:
            d: Dict[str, Any] = {
                "account_id": r.get("account_id") or "",
                "contract_key": r.get("contract_key") or "",
                "symbol": r.get("symbol") or "",
                "sec_type": r.get("sec_type") or "",
                "position": r.get("position"),
                "avg_cost": r.get("avg_cost"),
                "expiry": r.get("expiry"),
                "strike": r.get("strike"),
                "option_right": r.get("option_right"),
            }
            for price_key in ("price_mid", "price_last"):
                v = r.get(price_key)
                if v is not None:
                    try:
                        fv = float(v)
                        if math.isfinite(fv) and fv > 0:
                            d["price"] = fv
                            break
                    except (TypeError, ValueError):
                        pass
            result.append(d)
        return result
    except Exception as e:
        logger.warning("get_instance_open_option_legs failed: %s", e)
        return []
