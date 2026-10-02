"""Write strategy_opportunity symbols_json + entry_conditions_json. Used by POST/PUT opportunities API.

``patch_opportunity`` (core 0.33.0, TD-15) returns the row and raises ``Write*``;
``update_opportunity`` keeps its hybrid replace for one release."""

import json
import logging
from typing import Any, Dict, List, Optional

import psycopg2

from bifrost_core.monitor.reader import strategy as strategy_reader
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteFailed, WriteInvalid, WriteNotFound
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
        logger.warning("strategy_opportunity_write connect failed: %s", e)
        return None


def _normalize_symbols(value: Any) -> List[str]:
    """Return list of non-empty symbol strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [str(s).strip() for s in value if s is not None and str(s).strip()]


def _normalize_entry_conditions(value: Any) -> List[Dict[str, Any]]:
    """Return list of dicts with condition_type, value_text, value_numeric."""
    if value is None or not isinstance(value, list):
        return []
    out = []
    for i, item in enumerate(value):
        if not isinstance(item, dict) or not item.get("condition_type"):
            continue
        ct = str(item.get("condition_type") or "").strip()
        if not ct:
            continue
        out.append(
            {
                "condition_type": ct,
                "value_text": item.get("value_text") if item.get("value_text") is not None else None,
                "value_numeric": float(item["value_numeric"]) if item.get("value_numeric") is not None else None,
                "sort_order": i,
            }
        )
    return out


def _write_json_columns(
    cur: Any, strategy_opportunity_id: int, symbols: List[str], entry_conditions: List[Dict[str, Any]]
) -> None:
    cur.execute(
        """
        UPDATE strategy_opportunity
        SET symbols_json = %s::jsonb,
            entry_conditions_json = %s::jsonb,
            updated_at = now()
        WHERE strategy_opportunity_id = %s
        """,
        (json.dumps(symbols), json.dumps(entry_conditions), strategy_opportunity_id),
    )


def create_opportunity(status_config: Optional[dict], payload: Dict[str, Any]) -> Optional[int]:
    """Insert strategy_opportunity with jsonb symbols/entry_conditions. Returns strategy_opportunity_id or None."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    strategy_structure_id = payload.get("strategy_structure_id")
    if strategy_structure_id is None:
        raise ValueError("strategy_structure_id is required")
    try:
        strategy_structure_id = int(strategy_structure_id)
    except (TypeError, ValueError):
        raise ValueError("strategy_structure_id must be an integer")

    default_gate_safety_strategy_id = payload.get("default_gate_safety_strategy_id")
    if default_gate_safety_strategy_id is not None:
        try:
            default_gate_safety_strategy_id = int(default_gate_safety_strategy_id)
        except (TypeError, ValueError):
            default_gate_safety_strategy_id = None

    scope_type = (payload.get("scope_type") or "").strip() or None
    symbols = _normalize_symbols(payload.get("symbols"))
    entry_conditions = _normalize_entry_conditions(payload.get("entry_conditions"))
    is_active = bool(payload["is_active"]) if payload.get("is_active") is not None else True

    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_opportunity (
                    name, strategy_structure_id, default_gate_safety_strategy_id,
                    scope_type, is_active, symbols_json, entry_conditions_json
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                RETURNING strategy_opportunity_id
                """,
                (
                    name,
                    strategy_structure_id,
                    default_gate_safety_strategy_id,
                    scope_type,
                    is_active,
                    json.dumps(symbols),
                    json.dumps(entry_conditions),
                ),
            )
            row = cur.fetchone()
            oid = row[0] if row else None
        conn.commit()
        return int(oid) if oid is not None else None
    except (ValueError, TypeError) as e:
        logger.warning("create_opportunity validation failed: %s", e)
        raise
    except Exception as e:
        logger.warning("create_opportunity failed: %s", e)
        conn.rollback()
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_opportunity(
    status_config: Optional[dict], strategy_opportunity_id: int, payload: Dict[str, Any]
) -> bool:
    """Update strategy_opportunity and replace jsonb symbols/entry_conditions."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    strategy_structure_id = payload.get("strategy_structure_id")
    if strategy_structure_id is None:
        raise ValueError("strategy_structure_id is required")
    try:
        strategy_structure_id = int(strategy_structure_id)
    except (TypeError, ValueError):
        raise ValueError("strategy_structure_id must be an integer")

    default_gate_safety_strategy_id = payload.get("default_gate_safety_strategy_id")
    if default_gate_safety_strategy_id is not None:
        try:
            default_gate_safety_strategy_id = int(default_gate_safety_strategy_id)
        except (TypeError, ValueError):
            default_gate_safety_strategy_id = None

    scope_type = (payload.get("scope_type") or "").strip() or None
    symbols = _normalize_symbols(payload.get("symbols")) if "symbols" in payload else None
    entry_conditions = (
        _normalize_entry_conditions(payload.get("entry_conditions"))
        if "entry_conditions" in payload
        else None
    )
    is_active = bool(payload["is_active"]) if payload.get("is_active") is not None else True

    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE strategy_opportunity SET
                    name = %s, strategy_structure_id = %s, default_gate_safety_strategy_id = %s,
                    scope_type = %s, is_active = %s, updated_at = now()
                WHERE strategy_opportunity_id = %s
                """,
                (
                    name,
                    strategy_structure_id,
                    default_gate_safety_strategy_id,
                    scope_type,
                    is_active,
                    strategy_opportunity_id,
                ),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
            if symbols is None or entry_conditions is None:
                cur.execute(
                    """
                    SELECT symbols_json, entry_conditions_json
                    FROM strategy_opportunity WHERE strategy_opportunity_id = %s
                    """,
                    (strategy_opportunity_id,),
                )
                existing = cur.fetchone()
                if existing:
                    if symbols is None:
                        raw = existing[0]
                        if isinstance(raw, str):
                            symbols = json.loads(raw)
                        else:
                            symbols = list(raw or [])
                    if entry_conditions is None:
                        raw = existing[1]
                        if isinstance(raw, str):
                            entry_conditions = json.loads(raw)
                        else:
                            entry_conditions = list(raw or [])
            _write_json_columns(
                cur,
                strategy_opportunity_id,
                symbols or [],
                entry_conditions or [],
            )
        conn.commit()
        return True
    except (ValueError, TypeError) as e:
        logger.warning("update_opportunity validation failed: %s", e)
        raise
    except Exception as e:
        logger.warning("update_opportunity failed: %s", e)
        conn.rollback()
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --- TD-15 writer (core 0.33.0): return the row / raise Write* ----------------------

OPPORTUNITY_PATCHABLE = (
    "name",
    "strategy_structure_id",
    "default_gate_safety_strategy_id",
    "scope_type",
    "is_active",
    "symbols",
    "entry_conditions",
)
_CONDITION_KEYS = ("condition_type", "value_text", "value_numeric", "sort_order")


def _patch_symbols(value: Any) -> List[str]:
    items = ws.list_value(value, "symbols")
    out: List[str] = []
    for i, item in enumerate(items):
        out.append(ws.text(item, f"symbols[{i}]", nullable=False))  # type: ignore[arg-type]
    return out


def _patch_entry_conditions(value: Any) -> List[Dict[str, Any]]:
    items = ws.list_value(value, "entry_conditions")
    out: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        label = f"entry_conditions[{i}]"
        if not isinstance(item, dict):
            raise WriteInvalid(f"{label} must be an object.")
        unknown = sorted(str(k) for k in item if k not in _CONDITION_KEYS)
        if unknown:
            raise WriteInvalid(f"{label}: unknown field {', '.join(unknown)}.")
        value_text = item.get("value_text")
        if value_text is not None and not isinstance(value_text, str):
            raise WriteInvalid(f"{label}.value_text must be text or null.")
        out.append(
            {
                "condition_type": ws.text(item.get("condition_type"), f"{label}.condition_type", nullable=False),
                "value_text": value_text,
                "value_numeric": ws.number(item.get("value_numeric"), f"{label}.value_numeric", nullable=True),
                "sort_order": i,
            }
        )
    return out


def patch_opportunity(conn_or_config: Any, strategy_opportunity_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change the fields the client sent; return the row as ``strategy.get_opportunity_by_id`` reads it.

    ``name`` NOT NULL text · ``strategy_structure_id`` NOT NULL id · ``is_active`` boolean ·
    ``default_gate_safety_strategy_id`` nullable id · ``scope_type`` nullable text ·
    ``symbols`` (list of text) and ``entry_conditions`` (list of {condition_type, value_text,
    value_numeric}) replace the stored list (``[]`` empties it; null is refused).
    A field left out keeps its value -- unlike PUT, which NULLs the gate and scope and
    reactivates the rule. Raises WriteInvalid, WriteNotFound, WriteFailed.
    """
    what = f"opportunity {strategy_opportunity_id}"
    fields = ws.check_fields(fields, OPPORTUNITY_PATCHABLE, "opportunity")
    columns: Dict[str, Any] = {}
    if "name" in fields:
        columns["name"] = ws.text(fields["name"], "name", nullable=False)
    if "strategy_structure_id" in fields:
        columns["strategy_structure_id"] = ws.row_id(
            fields["strategy_structure_id"], "strategy_structure_id", nullable=False
        )
    if "default_gate_safety_strategy_id" in fields:
        columns["default_gate_safety_strategy_id"] = ws.row_id(
            fields["default_gate_safety_strategy_id"], "default_gate_safety_strategy_id", nullable=True
        )
    if "scope_type" in fields:
        columns["scope_type"] = ws.text(fields["scope_type"], "scope_type", nullable=True)
    if "is_active" in fields:
        columns["is_active"] = ws.boolean(fields["is_active"], "is_active")
    if "symbols" in fields:
        columns["symbols_json"] = json.dumps(_patch_symbols(fields["symbols"]))
    if "entry_conditions" in fields:
        columns["entry_conditions_json"] = json.dumps(_patch_entry_conditions(fields["entry_conditions"]))
    assignments, values = ws.set_clause(columns, jsonb=("symbols_json", "entry_conditions_json"))
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE strategy_opportunity SET {assignments} WHERE strategy_opportunity_id = %s",
                [*values, strategy_opportunity_id],
            )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No opportunity {strategy_opportunity_id}.")
        row = strategy_reader.get_opportunity_by_id(conn, strategy_opportunity_id)
        if row is None:
            raise WriteFailed(f"{what} was changed but could not be read back; nothing was saved.")
    return row
