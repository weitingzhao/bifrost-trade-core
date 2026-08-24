"""Write gate_safety_strategy.params_json. Used by POST/PUT gate-safety API."""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import psycopg2

from bifrost_core.monitor.schemas.gate_params import GateParams
from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)

_METADATA_COLUMNS = (
    "name",
    "version",
    "dim_direction",
    "dim_structure",
    "dim_coverage",
    "dim_risk",
    "dim_volatility",
    "dim_time",
    "is_active",
)


def _conn_from_config(status_config: Optional[dict]) -> Any:
    """Open a connection from status_config (postgres). Returns None if config invalid."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        params = _get_conn_params(status_config)
        return psycopg2.connect(**params)
    except Exception as e:
        logger.warning("gate_safety_write connect failed: %s", e)
        return None


def _payload_to_metadata_and_params(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Extract metadata row + validated params_json from API payload."""
    gates = dict(payload.get("gates") or {})
    strategy = dict(gates.get("strategy") or {})
    earnings = dict(strategy.get("earnings") or {})

    earnings_dates = payload.get("earnings_dates")
    if earnings_dates is None:
        earnings_dates = earnings.get("dates") or []
    earnings_dates = [str(d).strip()[:10] for d in earnings_dates if d]

    strategy["earnings"] = {**earnings, "dates": earnings_dates}
    gates["strategy"] = strategy
    params = GateParams.model_validate(gates).model_dump()

    def _dim(k: str) -> Optional[str]:
        v = payload.get(k)
        if v is None or str(v).strip() == "":
            return None
        return str(v).strip()

    metadata = {
        "name": (payload.get("name") or "").strip() or "Unnamed",
        "version": int(payload["version"]) if payload.get("version") is not None else 1,
        "dim_direction": _dim("dim_direction"),
        "dim_structure": _dim("dim_structure"),
        "dim_coverage": _dim("dim_coverage"),
        "dim_risk": _dim("dim_risk"),
        "dim_volatility": _dim("dim_volatility"),
        "dim_time": _dim("dim_time"),
        "is_active": bool(payload["is_active"]) if payload.get("is_active") is not None else True,
    }
    return metadata, params


# Back-compat for tests that import _payload_to_row / _STRATEGY_COLUMNS
def _payload_to_row(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    metadata, params = _payload_to_metadata_and_params(payload)
    row = {**metadata, "params_json": params}
    dates = params.get("strategy", {}).get("earnings", {}).get("dates") or []
    return row, list(dates)


_STRATEGY_COLUMNS = _METADATA_COLUMNS  # legacy test alias


def create_gate_safety(status_config: Optional[dict], payload: Dict[str, Any]) -> Optional[int]:
    """Insert a new gate_safety_strategy row. Returns id or None on error."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    try:
        metadata, params = _payload_to_metadata_and_params(payload)
        cols = ", ".join(_METADATA_COLUMNS) + ", params_json"
        placeholders = ", ".join(["%s"] * len(_METADATA_COLUMNS)) + ", %s::jsonb"
        values = tuple(metadata[c] for c in _METADATA_COLUMNS) + (json.dumps(params),)
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO gate_safety_strategy ({cols}) VALUES ({placeholders}) RETURNING gate_safety_strategy_id",
                values,
            )
            fetched = cur.fetchone()
            if not fetched:
                return None
            gid = int(fetched[0])
        conn.commit()
        return gid
    except Exception as e:
        logger.warning("create_gate_safety failed: %s", e)
        conn.rollback()
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_gate_safety(status_config: Optional[dict], gate_safety_strategy_id: int, payload: Dict[str, Any]) -> bool:
    """Update an existing gate_safety_strategy row. Returns True on success."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        metadata, params = _payload_to_metadata_and_params(payload)
        assignments = ", ".join(f"{c} = %s" for c in _METADATA_COLUMNS)
        values = tuple(metadata[c] for c in _METADATA_COLUMNS) + (
            json.dumps(params),
            gate_safety_strategy_id,
        )
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE gate_safety_strategy SET {assignments}, params_json = %s::jsonb, updated_at = now() "
                "WHERE gate_safety_strategy_id = %s",
                values,
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
        conn.commit()
        return True
    except Exception as e:
        logger.warning("update_gate_safety failed: %s", e)
        conn.rollback()
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
