"""Delete the Desk's rule objects: opportunities, allocations, gate sets.

The Trade Desk deletes without asking and offers Undo (design Rev .140): the
UI takes the card away at once and only calls here once its toast has closed
without Undo. So these are hard deletes, and every one of them first asks the
only question that can still stop it — is the object in use? An object in use
is refused with the reason, in words the Desk shows as they are:

- an opportunity with trades (``strategy_instance`` rows) — its trades would
  lose their rule. Its allocation memberships go with it (the junction
  cascades), and a plan that pointed at it keeps its text but drops the link.
- the allocation the daemon runs (`settings.active_strategy_allocation_id`,
  what Set active writes). An allocation merely on the books (`is_active`)
  may go — the daemon does not read it.
- a gate set that an opportunity defaults to, an allocation uses, or the
  daemon's settings point at.
"""

import logging
from typing import Any, Dict, Optional


from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteConflict, WriteNotFound

logger = logging.getLogger(__name__)


class RuleInUseError(WriteConflict, ValueError):
    """The object is referenced and cannot be deleted; ``reason`` says by what.

    A ``WriteConflict`` (409) since core 0.33.0; still a ``ValueError`` for the
    callers that catch it that way.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


def _conn_from_config(status_config: Optional[dict]) -> Any:
    """Open a connection from status_config (postgres). None when not configured or unreachable."""
    return ws.conn_from_config(status_config, "strategy_rules_delete", log=logger)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _delete(status_config: Optional[dict], table: str, key: str, row_id: int, check) -> bool:
    """Lock the row, run ``check`` (raises RuleInUseError), delete. False when absent."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {table} WHERE {key} = %s FOR UPDATE", (row_id,))
            if cur.fetchone() is None:
                conn.rollback()
                return False
            check(cur)
            cur.execute(f"DELETE FROM {table} WHERE {key} = %s", (row_id,))
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - rollback failure path
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - close failure path
            pass


def _check_opportunity(strategy_opportunity_id: int):
    def check(cur: Any) -> None:
        cur.execute(
            "SELECT count(*) FROM strategy_instance WHERE strategy_opportunity_id = %s",
            (strategy_opportunity_id,),
        )
        trades = int(cur.fetchone()[0])
        if trades:
            raise RuleInUseError(f"It has {_count(trades, 'trade', 'trades')}; a rule with trades stays.")

    return check


def delete_opportunity(status_config: Optional[dict], strategy_opportunity_id: int) -> bool:
    """Delete an opportunity that has no trades. Its allocation memberships go with it."""
    return _delete(
        status_config,
        "strategy_opportunity",
        "strategy_opportunity_id",
        strategy_opportunity_id,
        _check_opportunity(strategy_opportunity_id),
    )


def _daemon_setting(cur: Any, column: str) -> Optional[int]:
    """What the daemon's settings row points at (id = 1), or None."""
    cur.execute(f"SELECT {column} FROM settings WHERE id = 1")
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _check_allocation(strategy_allocation_id: int):
    def check(cur: Any) -> None:
        if _daemon_setting(cur, "active_strategy_allocation_id") == strategy_allocation_id:
            raise RuleInUseError("The daemon runs this allocation; set another one active first.")

    return check


def delete_allocation(status_config: Optional[dict], strategy_allocation_id: int) -> bool:
    """Delete an allocation the daemon does not run."""
    return _delete(
        status_config,
        "strategy_allocation",
        "strategy_allocation_id",
        strategy_allocation_id,
        _check_allocation(strategy_allocation_id),
    )


def _check_gate_safety(gate_safety_strategy_id: int):
    def check(cur: Any) -> None:
        cur.execute(
            "SELECT count(*) FROM strategy_opportunity WHERE default_gate_safety_strategy_id = %s",
            (gate_safety_strategy_id,),
        )
        opps = int(cur.fetchone()[0])
        cur.execute(
            "SELECT count(*) FROM strategy_allocation WHERE gate_safety_strategy_id = %s",
            (gate_safety_strategy_id,),
        )
        allocs = int(cur.fetchone()[0])
        if not opps and not allocs and _daemon_setting(cur, "active_gate_safety_strategy_id") == gate_safety_strategy_id:
            raise RuleInUseError("The daemon's settings use this gate set; point them at another one first.")
        if opps or allocs:
            users = []
            if opps:
                users.append(_count(opps, "opportunity", "opportunities"))
            if allocs:
                users.append(_count(allocs, "allocation", "allocations"))
            verb = "uses" if opps + allocs == 1 else "use"
            raise RuleInUseError(f"{' and '.join(users)} {verb} it; point them at another gate set first.")

    return check


def delete_gate_safety(status_config: Optional[dict], gate_safety_strategy_id: int) -> bool:
    """Delete a gate set that no opportunity defaults to and no allocation uses."""
    return _delete(
        status_config,
        "gate_safety_strategy",
        "gate_safety_strategy_id",
        gate_safety_strategy_id,
        _check_gate_safety(gate_safety_strategy_id),
    )


# --- TD-15 strict deletes (core 0.33.0): a dict or a Write* ---------------------------
#
# Same checks and the same reasons as above. What changes is what the caller can tell
# apart: a missing row is WriteNotFound (was False), in use is RuleInUseError -- a
# WriteConflict -- as before, and a database that is not configured, unreachable or
# failing is WriteFailed (was False, i.e. read as "not found", or an exception).


def _delete_strict(conn_or_config: Any, table: str, key: str, row_id: int, check, noun: str) -> Dict[str, Any]:
    what = f"{noun} {row_id}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what, on_fk="conflict"):
        with conn.cursor() as cur:
            cur.execute(f"SELECT 1 FROM {table} WHERE {key} = %s FOR UPDATE", (row_id,))
            if cur.fetchone() is None:
                raise WriteNotFound(f"No {noun} {row_id}.")
            check(cur)
            cur.execute(f"DELETE FROM {table} WHERE {key} = %s", (row_id,))
            if cur.rowcount == 0:
                raise WriteNotFound(f"No {noun} {row_id}.")
    return {"deleted": "hard", key: row_id}


def delete_opportunity_strict(conn_or_config: Any, strategy_opportunity_id: int) -> Dict[str, Any]:
    """``delete_opportunity`` with outcomes: ``{"deleted": "hard", "strategy_opportunity_id"}``,
    or WriteNotFound / RuleInUseError (WriteConflict: it has trades) / WriteFailed."""
    return _delete_strict(
        conn_or_config,
        "strategy_opportunity",
        "strategy_opportunity_id",
        strategy_opportunity_id,
        _check_opportunity(strategy_opportunity_id),
        "opportunity",
    )


def delete_allocation_strict(conn_or_config: Any, strategy_allocation_id: int) -> Dict[str, Any]:
    """``delete_allocation`` with outcomes: ``{"deleted": "hard", "strategy_allocation_id"}``,
    or WriteNotFound / RuleInUseError (the daemon runs it) / WriteFailed."""
    return _delete_strict(
        conn_or_config,
        "strategy_allocation",
        "strategy_allocation_id",
        strategy_allocation_id,
        _check_allocation(strategy_allocation_id),
        "allocation",
    )


def delete_gate_safety_strict(conn_or_config: Any, gate_safety_strategy_id: int) -> Dict[str, Any]:
    """``delete_gate_safety`` with outcomes: ``{"deleted": "hard", "gate_safety_strategy_id"}``,
    or WriteNotFound / RuleInUseError (opportunities, allocations or daemon settings use it) / WriteFailed."""
    return _delete_strict(
        conn_or_config,
        "gate_safety_strategy",
        "gate_safety_strategy_id",
        gate_safety_strategy_id,
        _check_gate_safety(gate_safety_strategy_id),
        "gate set",
    )
