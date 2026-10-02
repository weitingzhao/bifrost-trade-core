"""Write strategy_allocation and strategy_allocation_opportunity. Used by POST/PUT allocations API.

``patch_allocation`` (core 0.33.0, TD-15) returns the row and raises ``Write*``;
``update_allocation`` keeps answering a bool for one release."""

import logging
from typing import Any, Dict, List, Optional


from bifrost_core.monitor.reader import strategy as strategy_reader
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteFailed, WriteInvalid, WriteNotFound

logger = logging.getLogger(__name__)


def _conn_from_config(status_config: Optional[dict]) -> Any:
    """Open a connection from status_config (postgres). None when not configured or unreachable."""
    return ws.conn_from_config(status_config, "strategy_allocation_write", log=logger)


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


# --- TD-15 writer (core 0.33.0): return the row / raise Write* ----------------------

ALLOCATION_PATCHABLE = (
    "name",
    "gate_safety_strategy_id",
    "max_positions",
    "max_bp_pct",
    "allocation_limits",
    "is_active",
    "strategy_opportunity_ids",
)
_LIMIT_KEYS = ("max_positions", "max_bp_pct")


def _expand_allocation_limits(fields: Dict[str, Any]) -> Dict[str, Any]:
    """``allocation_limits`` is the PUT body's shape: an object of the two limits.

    In a PATCH its keys are patched one by one (a key left out keeps its value);
    ``allocation_limits: null`` clears both. Sending a limit both ways is refused.
    """
    if "allocation_limits" not in fields:
        return fields
    out = dict(fields)
    limits = out.pop("allocation_limits")
    if limits is None:
        limits = {k: None for k in _LIMIT_KEYS}
    if not isinstance(limits, dict):
        raise WriteInvalid("allocation_limits must be an object with max_positions and/or max_bp_pct.")
    unknown = sorted(str(k) for k in limits if k not in _LIMIT_KEYS)
    if unknown:
        raise WriteInvalid(f"Unknown allocation_limits field: {', '.join(unknown)}. Allowed: max_positions, max_bp_pct.")
    for key, value in limits.items():
        if key in out:
            raise WriteInvalid(f"{key} was sent both on its own and inside allocation_limits; send it once.")
        out[key] = value
    return out


def patch_allocation(conn_or_config: Any, strategy_allocation_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change the fields the client sent; return the row as ``strategy.get_allocation_by_id`` reads it.

    ``name`` NOT NULL text · ``is_active`` boolean · ``gate_safety_strategy_id`` nullable id ·
    ``max_positions`` nullable whole number >= 0 · ``max_bp_pct`` nullable number >= 0
    (or both through ``allocation_limits``) · ``strategy_opportunity_ids`` replaces the
    membership in the order given (``[]`` empties it; null is refused).
    Raises WriteInvalid (incl. an id that does not exist), WriteNotFound, WriteFailed.
    """
    what = f"allocation {strategy_allocation_id}"
    fields = ws.check_fields(fields, ALLOCATION_PATCHABLE, "allocation")
    fields = _expand_allocation_limits(fields)
    if not fields:
        raise WriteInvalid("Nothing to change: allocation_limits was empty.")
    columns: Dict[str, Any] = {}
    if "name" in fields:
        columns["name"] = ws.text(fields["name"], "name", nullable=False)
    if "gate_safety_strategy_id" in fields:
        columns["gate_safety_strategy_id"] = ws.row_id(
            fields["gate_safety_strategy_id"], "gate_safety_strategy_id", nullable=True
        )
    if "max_positions" in fields:
        columns["max_positions"] = ws.integer(fields["max_positions"], "max_positions", nullable=True, minimum=0)
    if "max_bp_pct" in fields:
        columns["max_bp_pct"] = ws.number(fields["max_bp_pct"], "max_bp_pct", nullable=True, minimum=0)
    if "is_active" in fields:
        columns["is_active"] = ws.boolean(fields["is_active"], "is_active")
    opportunity_ids: Optional[List[int]] = None
    if "strategy_opportunity_ids" in fields:
        raw = ws.list_value(fields["strategy_opportunity_ids"], "strategy_opportunity_ids")
        opportunity_ids = [ws.row_id(v, "strategy_opportunity_ids item", nullable=False) for v in raw]
        if len(set(opportunity_ids)) != len(opportunity_ids):
            raise WriteInvalid("strategy_opportunity_ids lists an opportunity twice.")
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            if columns:
                assignments, values = ws.set_clause(columns)
                cur.execute(
                    f"UPDATE strategy_allocation SET {assignments} WHERE strategy_allocation_id = %s",
                    [*values, strategy_allocation_id],
                )
            else:
                cur.execute(
                    "UPDATE strategy_allocation SET updated_at = now() WHERE strategy_allocation_id = %s",
                    (strategy_allocation_id,),
                )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No allocation {strategy_allocation_id}.")
            if opportunity_ids is not None:
                cur.execute(
                    "DELETE FROM strategy_allocation_opportunity WHERE strategy_allocation_id = %s",
                    (strategy_allocation_id,),
                )
                for i, oid in enumerate(opportunity_ids):
                    cur.execute(
                        "INSERT INTO strategy_allocation_opportunity "
                        "(strategy_allocation_id, strategy_opportunity_id, sort_order) VALUES (%s, %s, %s)",
                        (strategy_allocation_id, oid, i),
                    )
        row = strategy_reader.get_allocation_by_id(conn, strategy_allocation_id)
        if row is None:
            raise WriteFailed(f"{what} was changed but could not be read back; nothing was saved.")
    return row
