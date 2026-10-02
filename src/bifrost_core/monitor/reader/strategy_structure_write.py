"""Write strategy_structure and child tables. Used by POST/PUT structures API.

``patch_structure`` and ``delete_structure_strict`` (core 0.33.0, TD-15) raise the
``Write*`` outcomes; ``update_structure`` / ``deactivate_structure`` keep their
behaviour for one release."""

import json
import logging
from typing import Any, Dict, List, Optional


from bifrost_core.monitor.reader import strategy as strategy_reader
from bifrost_core.monitor.reader import structure_type_schema
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteFailed, WriteInvalid, WriteNotFound
from bifrost_core.monitor.reader import template_config

logger = logging.getLogger(__name__)

_LEG_ROLE_ALIASES: Dict[str, Dict[int, Dict[str, str]]] = {
    "covered_call": {
        0: {"stock": "underlying", "equity": "underlying"},
    },
}


def _normalize_key_for_aliases(template_code: str) -> str:
    tc = (template_code or "").strip().lower()
    if tc.startswith("covered_call"):
        return "covered_call"
    return tc


def _normalize_legs(
    template_code: str, legs: List[Any], schema: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    key = _normalize_key_for_aliases(template_code)
    if schema is not None:
        expected_legs = schema.get("legs", [])
    else:
        expected_legs = []
    aliases_by_index = _LEG_ROLE_ALIASES.get(key, {})
    if not isinstance(legs, list):
        return []
    out = []
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            out.append({})
            continue
        leg_copy = dict(leg)
        role_aliases = aliases_by_index.get(i, {})
        if role_aliases:
            r = leg_copy.get("role")
            if r is not None:
                r_str = str(r).strip().lower()
                if r_str in role_aliases:
                    leg_copy["role"] = role_aliases[r_str]
        if i < len(expected_legs):
            exp_role = expected_legs[i].get("role")
            if exp_role in ("call", "put"):
                got_role = (leg_copy.get("role") or "").strip().lower()
                if got_role == "option":
                    leg_copy["role"] = exp_role
        if i < len(expected_legs) and expected_legs[i].get("option_right") is None:
            leg_copy["option_right"] = None
        out.append(leg_copy)
    return out


def _conn_from_config(status_config: Optional[dict]) -> Any:
    """Open a connection from status_config (postgres). None when not configured or unreachable."""
    return ws.conn_from_config(status_config, "strategy_structure_write", log=logger)


def _write_legs_json(cur: Any, strategy_structure_id: int, legs: List[Dict[str, Any]]) -> None:
    legs_out: List[Dict[str, Any]] = []
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            continue
        legs_out.append(
            {
                "role": leg.get("role"),
                "direction": leg.get("direction"),
                "option_right": leg.get("option_right"),
                "quantity": int(leg["quantity"]) if leg.get("quantity") is not None else 1,
                "strike": float(leg["strike"]) if leg.get("strike") is not None else None,
                "expiration": (
                    str(leg["expiration"]).strip()
                    if leg.get("expiration") is not None
                    else None
                ),
                "sort_order": i,
            }
        )
    cur.execute(
        """
        UPDATE strategy_structure
        SET legs_json = %s::jsonb, updated_at = now()
        WHERE strategy_structure_id = %s
        """,
        (json.dumps(legs_out), strategy_structure_id),
    )


def _insert_meta(
    cur: Any, strategy_structure_id: int, meta: List[Dict[str, Any]]
) -> None:
    meta_obj: Dict[str, Any] = {}
    if meta and isinstance(meta, list):
        for m in meta:
            if not isinstance(m, dict) or not m.get("meta_key"):
                continue
            key = (m.get("meta_key") or "").strip()
            if not key:
                continue
            meta_obj[key] = m.get("meta_value_text")
    cur.execute(
        """
        UPDATE strategy_structure
        SET meta_json = %s::jsonb, updated_at = now()
        WHERE strategy_structure_id = %s
        """,
        (json.dumps(meta_obj), strategy_structure_id),
    )


def _resolve_template_id(
    conn: Any, payload: Dict[str, Any], existing_structure_id: Optional[int] = None
) -> tuple:
    """Return (strategy_template_id, template_row dict) or raise ValueError."""
    tid = payload.get("strategy_template_id")
    if tid is not None and str(tid).strip() != "":
        tid_int = int(tid)
        row = template_config.get_template_row(conn, tid_int)
        if not row:
            raise ValueError("strategy_template_id not found")
        return tid_int, row
    st = (payload.get("structure_type") or "").strip()
    if not st and existing_structure_id is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT strategy_template_id FROM strategy_structure WHERE strategy_structure_id = %s",
                    (existing_structure_id,),
                )
                r = cur.fetchone()
                if r and r[0]:
                    row = template_config.get_template_row(conn, int(r[0]))
                    if row:
                        return int(r[0]), row
        except Exception:
            pass
    if not st:
        raise ValueError("strategy_template_id or structure_type is required")
    sub = (payload.get("structure_subtype") or "").strip().lower() or None
    if st == "covered_call" and sub in ("otm", "atm", "itm", "deep_otm"):
        code = f"covered_call_{sub}"
    elif st == "covered_call":
        code = "covered_call_otm"
    else:
        code = st
    row = template_config.get_template_by_code(conn, code)
    if not row:
        raise ValueError(f"Unknown template for structure_type={st!r}")
    return int(row["strategy_template_id"]), row


def create_structure(
    status_config: Optional[dict], payload: Dict[str, Any]
) -> Optional[int]:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    legs = payload.get("legs")
    if legs is None:
        raise ValueError("legs is required")
    if not isinstance(legs, list):
        raise ValueError("legs must be an array")
    version = int(payload["version"]) if payload.get("version") is not None else 1
    is_active = (
        bool(payload["is_active"]) if payload.get("is_active") is not None else True
    )
    notes = (payload.get("notes") or "").strip() or None
    meta = payload.get("meta")
    if meta is not None and not isinstance(meta, list):
        raise ValueError("meta must be an array")

    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    try:
        tid, trow = _resolve_template_id(conn, payload, None)
        template_code = trow["template_code"]
        legs = template_config.get_template_legs(conn, tid)
        schema = structure_type_schema.build_schema_from_legs(legs)
        if schema and (schema.get("legs") or []):
            structure_type_schema.validate_legs(template_code, legs, schema=schema)
        legs = _normalize_legs(template_code, legs, schema)
        if schema and (schema.get("legs") or []):
            structure_type_schema.validate_legs(template_code, legs, schema=schema)

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_structure (
                    name, strategy_template_id, version, is_active, notes
                ) VALUES (%s,%s,%s,%s,%s)
                RETURNING strategy_structure_id
                """,
                (name, tid, version, is_active, notes),
            )
            row = cur.fetchone()
            if not row:
                return None
            sid = int(row[0])
            _write_legs_json(cur, sid, legs)
            _insert_meta(cur, sid, meta or [])
        conn.commit()
        return sid
    except (ValueError, TypeError) as e:
        logger.warning("create_structure validation failed: %s", e)
        raise
    except Exception as e:
        logger.warning("create_structure failed: %s", e)
        conn.rollback()
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_structure(
    status_config: Optional[dict], strategy_structure_id: int, payload: Dict[str, Any]
) -> bool:
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    legs_in = payload.get("legs")
    if legs_in is None:
        raise ValueError("legs is required")
    if not isinstance(legs_in, list):
        raise ValueError("legs must be an array")
    version = int(payload["version"]) if payload.get("version") is not None else 1
    is_active = (
        bool(payload["is_active"]) if payload.get("is_active") is not None else True
    )
    notes = (payload.get("notes") or "").strip() or None
    meta = payload.get("meta")
    if meta is not None and not isinstance(meta, list):
        raise ValueError("meta must be an array")

    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        tid, trow = _resolve_template_id(conn, payload)
        template_code = trow["template_code"]
        legs = template_config.get_template_legs(conn, tid)
        schema = structure_type_schema.build_schema_from_legs(legs)
        if schema and (schema.get("legs") or []):
            structure_type_schema.validate_legs(template_code, legs, schema=schema)
        legs = _normalize_legs(template_code, legs, schema)
        if schema and (schema.get("legs") or []):
            structure_type_schema.validate_legs(template_code, legs, schema=schema)

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE strategy_structure SET
                    name = %s, strategy_template_id = %s,
                    version = %s, is_active = %s, notes = %s, updated_at = now()
                WHERE strategy_structure_id = %s
                """,
                (
                    name,
                    tid,
                    version,
                    is_active,
                    notes,
                    strategy_structure_id,
                ),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
            _write_legs_json(cur, strategy_structure_id, legs)
            _insert_meta(cur, strategy_structure_id, meta or [])
        conn.commit()
        return True
    except (ValueError, TypeError) as e:
        logger.warning("update_structure validation failed: %s", e)
        raise
    except Exception as e:
        logger.warning("update_structure failed: %s", e)
        conn.rollback()
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def deactivate_structure(
    status_config: Optional[dict], strategy_structure_id: int
) -> bool:
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE strategy_structure SET is_active = false WHERE strategy_structure_id = %s",
                (strategy_structure_id,),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
            cur.execute(
                """
                UPDATE settings SET active_strategy_structure_id = NULL
                WHERE id = 1 AND active_strategy_structure_id = %s
                """,
                (strategy_structure_id,),
            )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("deactivate_structure failed: %s", e)
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --- TD-15 writers (core 0.33.0): return the row / raise Write* ---------------------

STRUCTURE_PATCHABLE = ("name", "version", "is_active", "notes", "meta")


def _patch_meta(value: Any) -> Dict[str, Any]:
    """``meta`` in the PUT body's shape -- [{meta_key, meta_value_text}] -- replacing meta_json."""
    items = ws.list_value(value, "meta")
    out: Dict[str, Any] = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise WriteInvalid(f"meta[{i}] must be an object with meta_key and meta_value_text.")
        unknown = sorted(str(k) for k in item if k not in ("meta_key", "meta_value_text"))
        if unknown:
            raise WriteInvalid(f"meta[{i}]: unknown field {', '.join(unknown)}.")
        key = ws.text(item.get("meta_key"), f"meta[{i}].meta_key", nullable=False) or ""
        if key in out:
            raise WriteInvalid(f"meta lists {key} twice.")
        val = item.get("meta_value_text")
        if val is not None and not isinstance(val, str):
            raise WriteInvalid(f"meta[{i}].meta_value_text must be text or null.")
        out[key] = val
    return out


def patch_structure(conn_or_config: Any, strategy_structure_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change the structure's own fields; return it as ``strategy.get_structure_by_id`` reads it.

    ``name`` NOT NULL text · ``version`` NOT NULL whole number >= 1 · ``is_active`` boolean ·
    ``notes`` nullable text · ``meta`` replaces meta_json ([{meta_key, meta_value_text}]; ``[]``
    empties it). The template and legs are not patchable (legs come from the template).
    ``is_active: false`` here only flips the column; ``delete_structure_strict`` also
    clears the daemon's active-structure setting. Raises WriteInvalid, WriteNotFound, WriteFailed.
    """
    what = f"structure {strategy_structure_id}"
    fields = ws.check_fields(fields, STRUCTURE_PATCHABLE, "structure")
    columns: Dict[str, Any] = {}
    if "name" in fields:
        columns["name"] = ws.text(fields["name"], "name", nullable=False)
    if "version" in fields:
        columns["version"] = ws.integer(fields["version"], "version", nullable=False, minimum=1)
    if "is_active" in fields:
        columns["is_active"] = ws.boolean(fields["is_active"], "is_active")
    if "notes" in fields:
        columns["notes"] = ws.text(fields["notes"], "notes", nullable=True)
    if "meta" in fields:
        columns["meta_json"] = json.dumps(_patch_meta(fields["meta"]))
    assignments, values = ws.set_clause(columns, jsonb=("meta_json",))
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE strategy_structure SET {assignments} WHERE strategy_structure_id = %s",
                [*values, strategy_structure_id],
            )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No structure {strategy_structure_id}.")
        row = strategy_reader.get_structure_by_id(conn, strategy_structure_id)
        if row is None:
            raise WriteFailed(f"{what} was changed but could not be read back; nothing was saved.")
    return row


def delete_structure_strict(conn_or_config: Any, strategy_structure_id: int) -> Dict[str, Any]:
    """Soft-delete: ``is_active = false``, and the daemon's settings stop pointing at it.

    Returns ``{"deleted": "soft", "strategy_structure_id", "was_active", "cleared_daemon_setting"}``.
    Structures are the one soft delete: opportunities and plans keep referencing the
    row. Nothing refuses it (no in-use check, as today). Raises WriteNotFound, WriteFailed.
    """
    what = f"structure {strategy_structure_id}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_active FROM strategy_structure WHERE strategy_structure_id = %s FOR UPDATE",
                (strategy_structure_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise WriteNotFound(f"No structure {strategy_structure_id}.")
            was_active = bool(row[0])
            cur.execute(
                "UPDATE strategy_structure SET is_active = false, updated_at = now() WHERE strategy_structure_id = %s",
                (strategy_structure_id,),
            )
            cur.execute(
                "UPDATE settings SET active_strategy_structure_id = NULL "
                "WHERE id = 1 AND active_strategy_structure_id = %s",
                (strategy_structure_id,),
            )
            cleared = cur.rowcount > 0
    return {
        "deleted": "soft",
        "strategy_structure_id": strategy_structure_id,
        "was_active": was_active,
        "cleared_daemon_setting": cleared,
    }
