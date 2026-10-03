"""Strategy instance CRUD: list, get, create, update, open-legs. Used for trade attribution (SI.2).

``patch_instance`` and ``delete_instance_strict`` (core 0.33.0, TD-15) raise the
``Write*`` outcomes; ``update_instance`` / ``delete_instance`` keep answering a
bool for one release."""

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from bifrost_core.portfolio.quote_freshness import fresh_quote_sql

from bifrost_core.persistence.postgres.brokerage_tables import (
    CONTRACT_QUOTE_LIVE,
    EXECUTIONS_FINAL,
    INSTANCE_ALLOCATION,
    INSTANCE_EXECUTION,
    POSITIONS,
)
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.instance_state import instance_states
from bifrost_core.monitor.reader.trade_names import add_trade_names
from bifrost_core.monitor.reader.errors import (
    ReadFailed,
    WriteConflict,
    WriteFailed,
    WriteInvalid,
    WriteNotFound,
)

logger = logging.getLogger(__name__)

_EXEC_READ_TABLE = EXECUTIONS_FINAL
_ALLOC_TABLE = INSTANCE_ALLOCATION


def list_instances(
    conn: Any,
    account_id: Optional[str] = None,
    strategy_opportunity_id: Optional[int] = None,
    strategy_instance_ids: Optional[List[int]] = None,
    opened_at_from: Optional[float] = None,
    opened_at_until: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """List strategy instances, optionally filtered by account_id, strategy_opportunity_id, strategy_instance_ids, opened_at range (Unix seconds).

    Each row carries ``state`` (no_fills / open / expired / closed) and ``closed_on`` (ISO date
    or None), derived from its option fills by ``instance_state`` (TD-43, core 0.41.0)."""
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
                # executions_count: one pass over the executions, then a GROUP BY.
                # It used to be a correlated subquery per instance whose
                # `WHERE … OR EXISTS (…)` defeated every index, so each instance
                # pulled the whole FDW table again — 4.6–7.0 s for 87 instances
                # on DEV (2026-09-28), over the reader's 5 s statement_timeout.
                # The timeout was swallowed below and the route answered HTTP 200
                # with an empty list: the "intermittent empty 200" recorded since
                # 2026-09-18. Same count (an allocation row counts only when its
                # execution is in the read table) — verified row for row, < 0.01 s.
                f"""
                WITH ex AS (
                    SELECT e.account_executions_id, e.strategy_instance_id FROM {_EXEC_READ_TABLE} e
                ),
                linked AS (
                    SELECT account_executions_id, strategy_instance_id AS sid
                    FROM ex WHERE strategy_instance_id IS NOT NULL
                    UNION
                    SELECT a.account_executions_id, a.strategy_instance_id
                    FROM {_ALLOC_TABLE} a
                    JOIN ex ON ex.account_executions_id = a.account_executions_id
                ),
                counts AS (
                    SELECT sid, COUNT(DISTINCT account_executions_id) AS n FROM linked GROUP BY sid
                )
                SELECT si.strategy_instance_id, si.strategy_opportunity_id, si.account_id,
                       si.opened_at, si.label, si.created_at, si.updated_at,
                       so.name AS strategy_opportunity_name,
                       ss.strategy_structure_id, ss.name AS strategy_structure_name,
                       COALESCE(c.n, 0) AS executions_count
                FROM strategy_instance si
                LEFT JOIN strategy_opportunity so ON si.strategy_opportunity_id = so.strategy_opportunity_id
                LEFT JOIN strategy_structure ss ON so.strategy_structure_id = ss.strategy_structure_id
                LEFT JOIN counts c ON c.sid = si.strategy_instance_id
                {where}
                ORDER BY si.opened_at DESC
                """,
                values,
            )
            rows = cur.fetchall()
            states = instance_states(cur, [int(r["strategy_instance_id"]) for r in rows])
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d.get("opened_at") is not None and hasattr(d["opened_at"], "timestamp"):
                d["opened_at_epoch"] = d["opened_at"].timestamp()
            if d.get("created_at") is not None and hasattr(d["created_at"], "timestamp"):
                d["created_at_epoch"] = d["created_at"].timestamp()
            if d.get("executions_count") is not None:
                d["executions_count"] = int(d["executions_count"])
            state, closed_on = states.get(int(d["strategy_instance_id"]), ("no_fills", None))
            d["state"] = state
            d["closed_on"] = closed_on.isoformat() if closed_on is not None else None
            out.append(add_trade_names(d))  # trade_id beside strategy_instance_id (naming R1)
        return out
    except Exception as e:
        # A failed read is not an empty one: raise, so the API answers 503 (TD-08).
        raise ReadFailed(f"list_instances: {e}") from e


def get_instance_by_id(conn: Any, strategy_instance_id: int) -> Optional[Dict[str, Any]]:
    """Return one strategy instance by id, or None."""
    if conn is None:
        return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT si.strategy_instance_id, si.strategy_opportunity_id, si.account_id,
                       si.opened_at, si.label, si.created_at, si.updated_at,
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
        return add_trade_names(d)
    except Exception as e:
        logger.debug("get_instance_by_id failed: %s", e)
        return None


def create_instance(
    conn: Any,
    strategy_opportunity_id: int,
    account_id: str,
    opened_at: Any,
    label: Optional[str] = None,
) -> Optional[int]:
    """Insert one strategy_instance. opened_at: datetime or Unix timestamp. Returns strategy_instance_id or None.

    No ``notes`` since core 0.43.0 (TD-73): a trade's notes live in the Research journal."""
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
                INSERT INTO strategy_instance (strategy_opportunity_id, account_id, opened_at, label, updated_at)
                VALUES (%s, %s, %s, %s, now())
                RETURNING strategy_instance_id
                """,
                (strategy_opportunity_id, account_id, opened_dt, label or None),
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
    created_at: Optional[Any] = None,
    opened_at: Optional[Any] = None,
) -> bool:
    """Update label, created_at, and/or opened_at of a strategy instance. created_at/opened_at: datetime or Unix timestamp. Returns True if a row was updated."""
    if conn is None:
        return False
    updates = []
    values: List[Any] = []
    if label is not None:
        updates.append("label = %s")
        values.append(label.strip() if isinstance(label, str) else label)
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
                FROM {POSITIONS} ap
                INNER JOIN (
                    SELECT DISTINCT account_id, contract_key
                    FROM {_EXEC_READ_TABLE}
                    WHERE strategy_instance_id = %s
                      AND upper(trim(COALESCE(sec_type, ''))) = 'OPT'
                ) tagged ON ap.account_id = tagged.account_id AND ap.contract_key = tagged.contract_key
                LEFT JOIN {CONTRACT_QUOTE_LIVE} ip
                    ON ap.contract_key = ip.contract_key AND {fresh_quote_sql('ip')}
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


# --- TD-15 writers (core 0.33.0): return the row / raise Write* --------------------

# No ``notes`` since core 0.43.0 (TD-73): a trade's notes live in the Research journal, and
# the column is dropped after this release (infra db-steps 2026-10-03-td43-td73-drop-columns).
# A ``notes`` key is refused (WriteInvalid) rather than dropped.
INSTANCE_PATCHABLE = ("label", "opened_at", "created_at")

NOTES_RETIRED = (
    "notes was removed in core 0.43.0 (TD-73): a trade's notes live in the Research journal "
    "(POST /research/journal/notes with a ref of type 'inst')."
)


def patch_instance(conn_or_config: Any, strategy_instance_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change the fields the client sent; return the row as ``get_instance_by_id`` reads it.

    ``label``: nullable text -- null clears, blank is refused.
    ``opened_at`` / ``created_at``: NOT NULL timestamps (datetime, Unix seconds or ISO 8601).
    Raises WriteInvalid (empty, unknown key, bad value), WriteNotFound, WriteFailed.
    """
    what = f"trade {strategy_instance_id}"
    if isinstance(fields, dict) and "notes" in fields:
        raise WriteInvalid(NOTES_RETIRED)
    fields = ws.check_fields(fields, INSTANCE_PATCHABLE, "trade")
    columns: Dict[str, Any] = {}
    if "label" in fields:
        columns["label"] = ws.text(fields["label"], "label", nullable=True)
    for name in ("opened_at", "created_at"):
        if name in fields:
            columns[name] = ws.timestamp(fields[name], name, nullable=False)
    assignments, values = ws.set_clause(columns)
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE strategy_instance SET {assignments} WHERE strategy_instance_id = %s",
                [*values, strategy_instance_id],
            )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No trade {strategy_instance_id}.")
        row = get_instance_by_id(conn, strategy_instance_id)
        if row is None:
            raise WriteFailed(f"{what} was changed but could not be read back; nothing was saved.")
    return row


def _attributed_counts(cur: Any, strategy_instance_id: int) -> Tuple[int, int]:
    """(whole fills, split fills) attributed to the instance in this env's strategy_instance_execution."""
    cur.execute(
        f"SELECT count(*) FILTER (WHERE allocated_quantity IS NULL), "
        f"count(*) FILTER (WHERE allocated_quantity IS NOT NULL) "
        f"FROM {INSTANCE_EXECUTION} WHERE strategy_instance_id = %s",
        (strategy_instance_id,),
    )
    row = cur.fetchone() or (0, 0)
    return int(row[0] or 0), int(row[1] or 0)


def _plan_and_review_counts(cur: Any, strategy_instance_id: int) -> Tuple[int, int]:
    """(plans filled by the instance, reviews of it): the two ON DELETE RESTRICT references (TD-43)."""
    cur.execute(
        "SELECT (SELECT count(*) FROM strategy_plan WHERE strategy_instance_id = %s), "
        "(SELECT count(*) FROM trade_review WHERE strategy_instance_id = %s)",
        (strategy_instance_id, strategy_instance_id),
    )
    row = cur.fetchone() or (0, 0)
    return int(row[0] or 0), int(row[1] or 0)


def count_attributed_executions(status_config: Any, strategy_instance_id: int) -> int:
    """Fills attributed whole to this instance (this env's strategy_instance_execution, TD-09).

    A fill is (account_id, exec_id), so one recorded by both TWS and Flex counts once.
    Before core 0.37.0 this read Golden Source's raw columns, shared by all three envs.
    Raises WriteFailed when the env database cannot be read.
    """
    what = f"the fills attributed to trade {strategy_instance_id}"
    with ws.write_connection(status_config, what) as conn:
        try:
            with conn.cursor() as cur:
                whole, _ = _attributed_counts(cur, strategy_instance_id)
            ws.rollback_quietly(conn)
        except Exception as e:
            ws.rollback_quietly(conn)
            logger.warning("count_attributed_executions(%s) failed: %s", strategy_instance_id, e)
            raise WriteFailed(f"Could not read {what}; nothing was deleted.") from e
    return whole


def delete_instance_strict(status_config: Any, strategy_instance_id: int) -> Dict[str, Any]:
    """Delete an instance nothing is attributed to. Returns ``{"deleted": "hard", "strategy_instance_id", "trade_id"}``.

    Refused (WriteConflict, nothing deleted) when fills are split-allocated to it or
    attributed to it whole in this env's ``strategy_instance_execution`` (TD-09; its
    FK is ON DELETE RESTRICT as well), and while a plan was filled by it or it has a
    review: both FKs are ON DELETE RESTRICT since core 0.41.0 (TD-43), so a filled plan never
    loses its instance and a review is never deleted with one.
    """
    what = f"trade {strategy_instance_id}"
    if not isinstance(status_config, dict):
        raise WriteFailed(
            f"Cannot delete {what}: the status config is needed to open its database."
        )
    with ws.write_connection(status_config, what) as conn, ws.write_transaction(conn, what, on_fk="conflict"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM strategy_instance WHERE strategy_instance_id = %s FOR UPDATE",
                (strategy_instance_id,),
            )
            if cur.fetchone() is None:
                raise WriteNotFound(f"No trade {strategy_instance_id}.")
            n_direct, n_split = _attributed_counts(cur, strategy_instance_id)
            if n_split:
                raise WriteConflict(
                    f"{ws.plural(n_split, 'fill is', 'fills are')} split to this trade; "
                    "move or clear those splits first."
                )
            if n_direct:
                raise WriteConflict(
                    f"{ws.plural(n_direct, 'fill is', 'fills are')} attributed to this trade."
                )
            n_plans, n_reviews = _plan_and_review_counts(cur, strategy_instance_id)
            if n_plans or n_reviews:
                held = [ws.plural(n_plans, "plan was", "plans were") + " filled by it"] if n_plans else []
                held += ["it has a review"] if n_reviews else []
                raise WriteConflict(f"Cannot delete instance {strategy_instance_id}: {' and '.join(held)}.")
            cur.execute(
                "DELETE FROM strategy_instance WHERE strategy_instance_id = %s",
                (strategy_instance_id,),
            )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No trade {strategy_instance_id}.")
    return {"deleted": "hard", "strategy_instance_id": strategy_instance_id, "trade_id": strategy_instance_id}
