"""Strategy instance CRUD: list, get, create, patch, strict delete. Used for trade attribution (SI.2).

``patch_instance`` and ``delete_instance_strict`` (core 0.33.0, TD-15) raise the
``Write*`` outcomes. The bool writers ``update_instance`` / ``delete_instance`` and
``get_instance_open_option_legs`` left in core 0.46.0 (TD-80: no caller)."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor


from bifrost_core.persistence.postgres.brokerage_tables import (
    EXECUTIONS_FINAL,
    TRADE_EXECUTION,
    TRADE_FILL_SPLITS,
)
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.instance_state import instance_states
from bifrost_core.monitor.reader.errors import (
    ReadFailed,
    WriteConflict,
    WriteFailed,
    WriteInvalid,
    WriteNotFound,
)

logger = logging.getLogger(__name__)

_EXEC_READ_TABLE = EXECUTIONS_FINAL
_ALLOC_TABLE = TRADE_FILL_SPLITS


def list_instances(
    conn: Any,
    account_id: Optional[str] = None,
    strategy_opportunity_id: Optional[int] = None,
    trade_ids: Optional[List[int]] = None,
    opened_at_from: Optional[float] = None,
    opened_at_until: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """List trades, optionally filtered by account_id, strategy_opportunity_id, trade_ids, opened_at range (Unix seconds).

    Rows carry ``trade_id`` only (``strategy_instance_id`` beside it from core 0.42.0 until
    0.47.0, naming R4).

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
        if trade_ids:
            placeholders = ", ".join(["%s"] * len(trade_ids))
            conditions.append(f"si.trade_id IN ({placeholders})")
            values.extend(trade_ids)
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
                    SELECT e.account_executions_id, e.trade_id FROM {_EXEC_READ_TABLE} e
                ),
                linked AS (
                    SELECT account_executions_id, trade_id AS sid
                    FROM ex WHERE trade_id IS NOT NULL
                    UNION
                    SELECT a.account_executions_id, a.trade_id
                    FROM {_ALLOC_TABLE} a
                    JOIN ex ON ex.account_executions_id = a.account_executions_id
                ),
                counts AS (
                    SELECT sid, COUNT(DISTINCT account_executions_id) AS n FROM linked GROUP BY sid
                )
                SELECT si.trade_id, si.strategy_opportunity_id, si.account_id,
                       si.opened_at, si.label, si.created_at, si.updated_at,
                       so.name AS strategy_opportunity_name,
                       ss.strategy_structure_id, ss.name AS strategy_structure_name,
                       COALESCE(c.n, 0) AS executions_count
                FROM trade si
                LEFT JOIN strategy_opportunity so ON si.strategy_opportunity_id = so.strategy_opportunity_id
                LEFT JOIN strategy_structure ss ON so.strategy_structure_id = ss.strategy_structure_id
                LEFT JOIN counts c ON c.sid = si.trade_id
                {where}
                ORDER BY si.opened_at DESC
                """,
                values,
            )
            rows = cur.fetchall()
            states = instance_states(cur, [int(r["trade_id"]) for r in rows])
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if d.get("opened_at") is not None and hasattr(d["opened_at"], "timestamp"):
                d["opened_at_epoch"] = d["opened_at"].timestamp()
            if d.get("created_at") is not None and hasattr(d["created_at"], "timestamp"):
                d["created_at_epoch"] = d["created_at"].timestamp()
            if d.get("executions_count") is not None:
                d["executions_count"] = int(d["executions_count"])
            state, closed_on = states.get(int(d["trade_id"]), ("no_fills", None))
            d["state"] = state
            d["closed_on"] = closed_on.isoformat() if closed_on is not None else None
            out.append(d)
        return out
    except Exception as e:
        # A failed read is not an empty one: raise, so the API answers 503 (TD-08).
        raise ReadFailed(f"list_instances: {e}") from e


def get_instance_by_id(conn: Any, trade_id: int) -> Optional[Dict[str, Any]]:
    """Return one strategy instance by id, or None."""
    if conn is None:
        return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT si.trade_id, si.strategy_opportunity_id, si.account_id,
                       si.opened_at, si.label, si.created_at, si.updated_at,
                       so.name AS strategy_opportunity_name,
                       ss.strategy_structure_id, ss.name AS strategy_structure_name
                FROM trade si
                LEFT JOIN strategy_opportunity so ON si.strategy_opportunity_id = so.strategy_opportunity_id
                LEFT JOIN strategy_structure ss ON so.strategy_structure_id = ss.strategy_structure_id
                WHERE si.trade_id = %s
                """,
                (trade_id,),
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
) -> Optional[int]:
    """Insert one trade (table trade, R3). opened_at: datetime or Unix timestamp. Returns trade_id or None.

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
                INSERT INTO trade (strategy_opportunity_id, account_id, opened_at, label, updated_at)
                VALUES (%s, %s, %s, %s, now())
                RETURNING trade_id
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


# --- TD-15 writers (core 0.33.0): return the row / raise Write* --------------------

# No ``notes`` since core 0.43.0 (TD-73): a trade's notes live in the Research journal, and
# the column is dropped after this release (infra db-steps 2026-10-03-td43-td73-drop-columns).
# A ``notes`` key is refused (WriteInvalid) rather than dropped.
INSTANCE_PATCHABLE = ("label", "opened_at", "created_at")

NOTES_RETIRED = (
    "notes was removed in core 0.43.0 (TD-73): a trade's notes live in the Research journal "
    "(POST /research/journal/notes with a ref of type 'trade')."
)


def patch_instance(conn_or_config: Any, trade_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change the fields the client sent; return the row as ``get_instance_by_id`` reads it.

    ``label``: nullable text -- null clears, blank is refused.
    ``opened_at`` / ``created_at``: NOT NULL timestamps (datetime, Unix seconds or ISO 8601).
    Raises WriteInvalid (empty, unknown key, bad value), WriteNotFound, WriteFailed.
    """
    what = f"trade {trade_id}"
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
                f"UPDATE trade SET {assignments} WHERE trade_id = %s",
                [*values, trade_id],
            )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No trade {trade_id}.")
        row = get_instance_by_id(conn, trade_id)
        if row is None:
            raise WriteFailed(f"{what} was changed but could not be read back; nothing was saved.")
    return row


def _attributed_counts(cur: Any, trade_id: int) -> Tuple[int, int]:
    """(whole fills, split fills) attributed to the trade in this env's trade_execution."""
    cur.execute(
        f"SELECT count(*) FILTER (WHERE split_quantity IS NULL), "
        f"count(*) FILTER (WHERE split_quantity IS NOT NULL) "
        f"FROM {TRADE_EXECUTION} WHERE trade_id = %s",
        (trade_id,),
    )
    row = cur.fetchone() or (0, 0)
    return int(row[0] or 0), int(row[1] or 0)


def _plan_and_review_counts(cur: Any, trade_id: int) -> Tuple[int, int]:
    """(plans filled by the instance, reviews of it): the two ON DELETE RESTRICT references (TD-43)."""
    cur.execute(
        "SELECT (SELECT count(*) FROM strategy_plan WHERE trade_id = %s), "
        "(SELECT count(*) FROM trade_review WHERE trade_id = %s)",
        (trade_id, trade_id),
    )
    row = cur.fetchone() or (0, 0)
    return int(row[0] or 0), int(row[1] or 0)


def count_attributed_executions(status_config: Any, trade_id: int) -> int:
    """Fills attributed whole to this trade (this env's trade_execution, TD-09).

    A fill is (account_id, exec_id), so one recorded by both TWS and Flex counts once.
    Before core 0.37.0 this read Golden Source's raw columns, shared by all three envs.
    Raises WriteFailed when the env database cannot be read.
    """
    what = f"the fills attributed to trade {trade_id}"
    with ws.write_connection(status_config, what) as conn:
        try:
            with conn.cursor() as cur:
                whole, _ = _attributed_counts(cur, trade_id)
            ws.rollback_quietly(conn)
        except Exception as e:
            ws.rollback_quietly(conn)
            logger.warning("count_attributed_executions(%s) failed: %s", trade_id, e)
            raise WriteFailed(f"Could not read {what}; nothing was deleted.") from e
    return whole


def delete_instance_strict(status_config: Any, trade_id: int) -> Dict[str, Any]:
    """Delete a trade nothing is attributed to. Returns ``{"deleted": "hard", "trade_id"}``
    (``strategy_instance_id`` beside it before core 0.47.0, naming R4).

    Refused (WriteConflict, nothing deleted) when fills are split-allocated to it or
    attributed to it whole in this env's ``trade_execution`` (TD-09; its
    FK is ON DELETE RESTRICT as well), and while a plan was filled by it or it has a
    review: both FKs are ON DELETE RESTRICT since core 0.41.0 (TD-43), so a filled plan never
    loses its instance and a review is never deleted with one.
    """
    what = f"trade {trade_id}"
    if not isinstance(status_config, dict):
        raise WriteFailed(
            f"Cannot delete {what}: the status config is needed to open its database."
        )
    with ws.write_connection(status_config, what) as conn, ws.write_transaction(conn, what, on_fk="conflict"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM trade WHERE trade_id = %s FOR UPDATE",
                (trade_id,),
            )
            if cur.fetchone() is None:
                raise WriteNotFound(f"No trade {trade_id}.")
            n_direct, n_split = _attributed_counts(cur, trade_id)
            if n_split:
                raise WriteConflict(
                    f"{ws.plural(n_split, 'fill is', 'fills are')} split to this trade; "
                    "move or clear those splits first."
                )
            if n_direct:
                raise WriteConflict(
                    f"{ws.plural(n_direct, 'fill is', 'fills are')} attributed to this trade."
                )
            n_plans, n_reviews = _plan_and_review_counts(cur, trade_id)
            if n_plans or n_reviews:
                held = [ws.plural(n_plans, "plan was", "plans were") + " filled by it"] if n_plans else []
                held += ["it has a review"] if n_reviews else []
                raise WriteConflict(f"Cannot delete instance {trade_id}: {' and '.join(held)}.")
            cur.execute(
                "DELETE FROM trade WHERE trade_id = %s",
                (trade_id,),
            )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No trade {trade_id}.")
    return {"deleted": "hard", "trade_id": trade_id}
