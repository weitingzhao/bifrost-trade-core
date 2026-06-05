"""Write strategy_allocation and strategy_allocation_opportunity. Used by POST/PUT allocations API."""

import logging
from typing import Any, Dict, List, Optional

import psycopg2

from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)


def _conn_from_config(status_config: Optional[dict]) -> Any:
    """Open a connection from status_config (postgres). Returns None if config invalid."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        params = _get_conn_params(status_config)
        return psycopg2.connect(**params)
    except Exception as e:
        logger.warning("strategy_allocation_write connect failed: %s", e)
        return None


def _normalize_opportunity_ids(value: Any) -> List[int]:
    """Return list of int (strategy_opportunity_id)."""
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    out = []
    for s in value:
        try:
            out.append(int(s))
        except (TypeError, ValueError):
            continue
    return out


def _limits_to_scalars(allocation_limits: Any) -> tuple:
    """Return (max_positions, max_bp_pct) from allocation_limits dict. Either can be None."""
    max_positions = None
    max_bp_pct = None
    if allocation_limits is not None and isinstance(allocation_limits, dict):
        if "max_positions" in allocation_limits and allocation_limits["max_positions"] is not None:
            try:
                max_positions = int(allocation_limits["max_positions"])
            except (TypeError, ValueError):
                pass
        if "max_bp_pct" in allocation_limits and allocation_limits["max_bp_pct"] is not None:
            try:
                max_bp_pct = float(allocation_limits["max_bp_pct"])
            except (TypeError, ValueError):
                pass
    return max_positions, max_bp_pct


def create_allocation(status_config: Optional[dict], payload: Dict[str, Any]) -> Optional[int]:
    """Insert strategy_allocation and strategy_allocation_opportunity. Returns strategy_allocation_id or None."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    if "strategy_opportunity_ids" not in payload:
        raise ValueError("strategy_opportunity_ids is required")
    if not isinstance(payload.get("strategy_opportunity_ids"), list):
        raise ValueError("strategy_opportunity_ids must be a list")
    opportunity_ids = _normalize_opportunity_ids(payload["strategy_opportunity_ids"])

    gate_safety_strategy_id = payload.get("gate_safety_strategy_id")
    if gate_safety_strategy_id is not None:
        try:
            gate_safety_strategy_id = int(gate_safety_strategy_id)
        except (TypeError, ValueError):
            gate_safety_strategy_id = None

    max_positions, max_bp_pct = _limits_to_scalars(payload.get("allocation_limits"))
    is_active = bool(payload["is_active"]) if payload.get("is_active") is not None else True

    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_allocation (
                    name, gate_safety_strategy_id, max_positions, max_bp_pct, is_active
                ) VALUES (%s, %s, %s, %s, %s)
                RETURNING strategy_allocation_id
                """,
                (name, gate_safety_strategy_id, max_positions, max_bp_pct, is_active),
            )
            row = cur.fetchone()
            aid = row[0] if row else None
            if aid is not None:
                for i, oid in enumerate(opportunity_ids):
                    cur.execute(
                        """
                        INSERT INTO strategy_allocation_opportunity (strategy_allocation_id, strategy_opportunity_id, sort_order)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (strategy_allocation_id, strategy_opportunity_id) DO UPDATE SET sort_order = EXCLUDED.sort_order
                        """,
                        (int(aid), oid, i),
                    )
        conn.commit()
        return int(aid) if aid is not None else None
    except (ValueError, TypeError) as e:
        logger.warning("create_allocation validation failed: %s", e)
        raise
    except Exception as e:
        logger.warning("create_allocation failed: %s", e)
        conn.rollback()
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_allocation(
    status_config: Optional[dict], strategy_allocation_id: int, payload: Dict[str, Any]
) -> bool:
    """Update strategy_allocation and optionally strategy_allocation_opportunity. Returns True if found and updated."""
    if not payload:
        return False
    name = (payload.get("name") or "").strip() if payload.get("name") is not None else None
    if name is not None and name == "":
        raise ValueError("name cannot be empty when provided")

    opportunity_ids = None
    if "strategy_opportunity_ids" in payload:
        opportunity_ids = _normalize_opportunity_ids(payload["strategy_opportunity_ids"])

    gate_safety_strategy_id = payload.get("gate_safety_strategy_id")
    if gate_safety_strategy_id is not None:
        try:
            gate_safety_strategy_id = int(gate_safety_strategy_id)
        except (TypeError, ValueError):
            gate_safety_strategy_id = None

    max_positions, max_bp_pct = None, None
    if "allocation_limits" in payload:
        max_positions, max_bp_pct = _limits_to_scalars(payload.get("allocation_limits"))

    is_active = payload.get("is_active")
    if is_active is not None:
        is_active = bool(is_active)

    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            updates = []
            params = []
            if name is not None:
                updates.append("name = %s")
                params.append(name)
            if "gate_safety_strategy_id" in payload:
                updates.append("gate_safety_strategy_id = %s")
                params.append(gate_safety_strategy_id)
            if "allocation_limits" in payload:
                updates.append("max_positions = %s")
                params.append(max_positions)
                updates.append("max_bp_pct = %s")
                params.append(max_bp_pct)
            if is_active is not None:
                updates.append("is_active = %s")
                params.append(is_active)
            if updates:
                updates.append("updated_at = now()")
                params.append(strategy_allocation_id)
                cur.execute(
                    f"UPDATE strategy_allocation SET {', '.join(updates)} WHERE strategy_allocation_id = %s",
                    params,
                )
                if cur.rowcount == 0:
                    conn.rollback()
                    return False
            if opportunity_ids is not None:
                cur.execute(
                    "DELETE FROM strategy_allocation_opportunity WHERE strategy_allocation_id = %s",
                    (strategy_allocation_id,),
                )
                for i, oid in enumerate(opportunity_ids):
                    cur.execute(
                        """
                        INSERT INTO strategy_allocation_opportunity (strategy_allocation_id, strategy_opportunity_id, sort_order)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (strategy_allocation_id, strategy_opportunity_id) DO UPDATE SET sort_order = EXCLUDED.sort_order
                        """,
                        (strategy_allocation_id, oid, i),
                    )
        conn.commit()
        return True
    except (ValueError, TypeError) as e:
        logger.warning("update_allocation validation failed: %s", e)
        raise
    except Exception as e:
        logger.warning("update_allocation failed: %s", e)
        conn.rollback()
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
