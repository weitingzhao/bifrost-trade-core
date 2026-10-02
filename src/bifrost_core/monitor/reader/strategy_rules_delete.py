"""Delete the Desk's rule objects: opportunities, allocations, gate sets.

The Trade Desk deletes without asking and offers Undo (design Rev .140): the
UI takes the card away at once and only calls here once its toast has closed
without Undo. So these are hard deletes, and every one of them first asks the
only question that can still stop it — is the object in use? An object in use
is refused with the reason, in words the Desk shows as they are:

- an opportunity with trades (``strategy_instance`` rows) — its trades would
  lose their rule. Its allocation memberships go with it (the junction
  cascades), and a plan that pointed at it keeps its text but drops the link.
- an allocation that is active — the one the daemon reads.
- a gate set that an opportunity defaults to or an allocation uses.
"""

import logging
from typing import Any, Optional

import psycopg2

from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)


class RuleInUseError(ValueError):
    """The object is referenced and cannot be deleted; ``reason`` says by what."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _conn_from_config(status_config: Optional[dict]) -> Any:
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        return psycopg2.connect(**_get_conn_params(status_config))
    except Exception as e:  # pragma: no cover - connection failure path
        logger.warning("strategy_rules_delete connect failed: %s", e)
        return None


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


def delete_opportunity(status_config: Optional[dict], strategy_opportunity_id: int) -> bool:
    """Delete an opportunity that has no trades. Its allocation memberships go with it."""

    def check(cur: Any) -> None:
        cur.execute(
            "SELECT count(*) FROM strategy_instance WHERE strategy_opportunity_id = %s",
            (strategy_opportunity_id,),
        )
        trades = int(cur.fetchone()[0])
        if trades:
            raise RuleInUseError(f"It has {_count(trades, 'trade', 'trades')}; a rule with trades stays.")

    return _delete(status_config, "strategy_opportunity", "strategy_opportunity_id", strategy_opportunity_id, check)


def delete_allocation(status_config: Optional[dict], strategy_allocation_id: int) -> bool:
    """Delete an allocation that is not the active one."""

    def check(cur: Any) -> None:
        cur.execute(
            "SELECT is_active FROM strategy_allocation WHERE strategy_allocation_id = %s",
            (strategy_allocation_id,),
        )
        if bool(cur.fetchone()[0]):
            raise RuleInUseError("It is the active allocation; set another one active first.")

    return _delete(status_config, "strategy_allocation", "strategy_allocation_id", strategy_allocation_id, check)


def delete_gate_safety(status_config: Optional[dict], gate_safety_strategy_id: int) -> bool:
    """Delete a gate set that no opportunity defaults to and no allocation uses."""

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
        if opps or allocs:
            users = []
            if opps:
                users.append(_count(opps, "opportunity", "opportunities"))
            if allocs:
                users.append(_count(allocs, "allocation", "allocations"))
            verb = "uses" if opps + allocs == 1 else "use"
            raise RuleInUseError(f"{' and '.join(users)} {verb} it; point them at another gate set first.")

    return _delete(status_config, "gate_safety_strategy", "gate_safety_strategy_id", gate_safety_strategy_id, check)
