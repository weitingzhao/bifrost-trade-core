"""Write gate_safety_strategy.params_json. Used by POST/PUT gate-safety API.

``patch_gate_safety`` (core 0.33.0, TD-15) returns the row and raises ``Write*``;
``update_gate_safety`` keeps its full replace for one release."""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor
from pydantic import ValidationError

from bifrost_core.monitor.reader import gate_safety as gate_safety_reader
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteFailed, WriteInvalid, WriteNotFound
from bifrost_core.monitor.reader.strategy_dim_catalog import is_valid_dim_code, validate_dim_fields
from bifrost_core.monitor.schemas.gate_params import GateParams

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
    """Open a connection from status_config (postgres). None when not configured or unreachable."""
    return ws.conn_from_config(status_config, "gate_safety_write", log=logger)


def _payload_to_metadata_and_params(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Extract metadata row + validated params_json from API payload.

    Earnings dates come only from the top-level `earnings_dates`. A non-empty
    gates.strategy.earnings.dates raises ValueError (the API answers 400) rather than
    being merged or silently dropped. params_json still stores the dates at
    strategy.earnings.dates, where the daemon's config['gates'] reads them.
    """
    gates = dict(payload.get("gates") or {})
    strategy = dict(gates.get("strategy") or {})
    earnings = dict(strategy.get("earnings") or {})

    if earnings.pop("dates", None):
        raise ValueError(
            "earnings dates belong in the top-level earnings_dates field, "
            "not in gates.strategy.earnings.dates"
        )
    earnings_dates = payload.get("earnings_dates")
    if earnings_dates is None:
        earnings_dates = []
    if not isinstance(earnings_dates, list):
        raise ValueError("earnings_dates must be an array of YYYY-MM-DD strings")
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
    validate_dim_fields(metadata)
    return metadata, params


# Back-compat for tests that import _payload_to_row / _STRATEGY_COLUMNS
def _payload_to_row(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    metadata, params = _payload_to_metadata_and_params(payload)
    row = {**metadata, "params_json": params}
    dates = params.get("strategy", {}).get("earnings", {}).get("dates") or []
    return row, list(dates)


_STRATEGY_COLUMNS = _METADATA_COLUMNS  # legacy test alias


def create_gate_safety(status_config: Optional[dict], payload: Dict[str, Any]) -> Optional[int]:
    """Insert a new gate_safety_strategy row. Returns id or None on a database error.

    A payload the gate params or the dim catalog refuse raises ValueError before
    any connection is opened; the API answers that with 400, not 500.
    """
    metadata, params = _payload_to_metadata_and_params(payload)
    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    try:
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
    """Update an existing gate_safety_strategy row. Returns True on success.

    A payload the gate params or the dim catalog refuse raises ValueError before
    any connection is opened, as in create_gate_safety.
    """
    metadata, params = _payload_to_metadata_and_params(payload)
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
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


# --- TD-15 writer (core 0.33.0): return the row / raise Write* ----------------------

_GATE_DIM_FIELDS = ("dim_direction", "dim_structure", "dim_coverage", "dim_risk", "dim_volatility", "dim_time")
GATE_SAFETY_PATCHABLE = ("name", "version", *_GATE_DIM_FIELDS, "is_active", "gates", "earnings_dates")


def _unknown_gate_paths(given: Any, reference: Any, prefix: str = "") -> List[str]:
    """Keys in ``given`` that the GateParams shape does not have (pydantic would drop them silently)."""
    if not isinstance(given, dict) or not isinstance(reference, dict):
        return []
    out: List[str] = []
    for key, value in given.items():
        path = f"{prefix}{key}"
        if key not in reference:
            out.append(path)
        elif isinstance(reference[key], dict):
            if not isinstance(value, dict):
                out.append(f"{path} (must be an object)")
            else:
                out.extend(_unknown_gate_paths(value, reference[key], f"{path}."))
    return out


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _patch_earnings_dates(value: Any) -> List[str]:
    items = ws.list_value(value, "earnings_dates")
    out: List[str] = []
    for i, item in enumerate(items):
        text = ws.text(item, f"earnings_dates[{i}]", nullable=False) or ""
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            raise WriteInvalid(f"earnings_dates[{i}] must be a date (YYYY-MM-DD).") from None
        out.append(text)
    return out


def patch_gate_safety(conn_or_config: Any, gate_safety_strategy_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change the fields the client sent; return the set as ``gate_safety.get_gate_safety_full_by_id`` reads it.

    ``name`` NOT NULL text · ``version`` NOT NULL whole number >= 1 · ``is_active`` boolean ·
    ``dim_*`` nullable catalog codes · ``gates``: a partial object deep-merged into the
    stored params (keys it leaves out keep their values; an unknown key is refused,
    and so are earnings dates inside it) · ``earnings_dates``: replaces the list
    (``[]`` empties it). The merged params are validated by GateParams before writing.
    Raises WriteInvalid, WriteNotFound, WriteFailed.
    """
    what = f"gate set {gate_safety_strategy_id}"
    fields = ws.check_fields(fields, GATE_SAFETY_PATCHABLE, "gate set")
    columns: Dict[str, Any] = {}
    if "name" in fields:
        columns["name"] = ws.text(fields["name"], "name", nullable=False)
    if "version" in fields:
        columns["version"] = ws.integer(fields["version"], "version", nullable=False, minimum=1)
    for name in _GATE_DIM_FIELDS:
        if name in fields:
            code = ws.text(fields[name], name, nullable=True)
            if code is not None and not is_valid_dim_code(name[len("dim_"):], code):
                raise WriteInvalid(f"{name}: {code} is not a code in the dim catalog.")
            columns[name] = code
    if "is_active" in fields:
        columns["is_active"] = ws.boolean(fields["is_active"], "is_active")
    gates_patch: Optional[Dict[str, Any]] = None
    if "gates" in fields:
        gates_patch = fields["gates"]
        if not isinstance(gates_patch, dict):
            raise WriteInvalid("gates must be an object.")
        unknown = _unknown_gate_paths(gates_patch, GateParams().model_dump())
        if unknown:
            raise WriteInvalid(f"Unknown gates field: {', '.join(unknown)}.")
        earnings = ((gates_patch.get("strategy") or {}).get("earnings") or {})
        if "dates" in earnings:
            raise WriteInvalid(
                "earnings dates belong in the top-level earnings_dates field, not in gates.strategy.earnings.dates."
            )
    dates = _patch_earnings_dates(fields["earnings_dates"]) if "earnings_dates" in fields else None
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        if gates_patch is not None or dates is not None:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT params_json FROM gate_safety_strategy WHERE gate_safety_strategy_id = %s FOR UPDATE",
                    (gate_safety_strategy_id,),
                )
                current = cur.fetchone()
            if current is None:
                raise WriteNotFound(f"No gate set {gate_safety_strategy_id}.")
            params = gate_safety_reader._parse_params_json(current.get("params_json"))
            if gates_patch is not None:
                params = _deep_merge(params, gates_patch)
            if dates is not None:
                params.setdefault("strategy", {}).setdefault("earnings", {})["dates"] = dates
            try:
                columns["params_json"] = json.dumps(GateParams.model_validate(params).model_dump(mode="json"))
            except ValidationError as e:
                first = e.errors()[0] if e.errors() else {}
                where = ".".join(str(p) for p in first.get("loc", ()))
                raise WriteInvalid(f"gates.{where}: {first.get('msg', 'invalid value')}.") from None
        assignments, values = ws.set_clause(columns, jsonb=("params_json",))
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE gate_safety_strategy SET {assignments} WHERE gate_safety_strategy_id = %s",
                [*values, gate_safety_strategy_id],
            )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No gate set {gate_safety_strategy_id}.")
        row = gate_safety_reader.get_gate_safety_full_by_id(conn, gate_safety_strategy_id)
        if row is None:
            raise WriteFailed(f"{what} was changed but could not be read back; nothing was saved.")
    return row
