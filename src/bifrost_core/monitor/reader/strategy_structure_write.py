"""Write strategy_structure and child tables. Used by POST/PUT structures API."""

import logging
from typing import Any, Dict, List, Optional

import psycopg2

from bifrost_core.monitor.reader import structure_type_schema
from bifrost_core.monitor.reader import template_config
from bifrost_core.persistence.postgres.connection import _get_conn_params

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
    if not status_config or (
        status_config.get("sink") != "postgres" and not status_config.get("postgres")
    ):
        return None
    try:
        params = _get_conn_params(status_config)
        return psycopg2.connect(**params)
    except Exception as e:
        logger.warning("strategy_structure_write connect failed: %s", e)
        return None


def _insert_legs(
    cur: Any, strategy_structure_id: int, legs: List[Dict[str, Any]]
) -> None:
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            continue
        cur.execute(
            """
            INSERT INTO strategy_structure_leg (
                strategy_structure_id, sort_order, role, direction, option_right,
                quantity, strike, expiration
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                strategy_structure_id,
                i,
                leg.get("role"),
                leg.get("direction"),
                leg.get("option_right"),
                int(leg["quantity"]) if leg.get("quantity") is not None else 1,
                float(leg["strike"]) if leg.get("strike") is not None else None,
                (
                    str(leg["expiration"]).strip()
                    if leg.get("expiration") is not None
                    else None
                ),
            ),
        )


def _insert_meta(
    cur: Any, strategy_structure_id: int, meta: List[Dict[str, Any]]
) -> None:
    if not meta or not isinstance(meta, list):
        return
    for m in meta:
        if not isinstance(m, dict) or not m.get("meta_key"):
            continue
        cur.execute(
            """
            INSERT INTO strategy_structure_meta (
                strategy_structure_id, meta_key, meta_value_text
            ) VALUES (%s, %s, %s)
            """,
            (
                strategy_structure_id,
                (m.get("meta_key") or "").strip(),
                m.get("meta_value_text"),
            ),
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
            _insert_legs(cur, sid, legs)
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
            cur.execute(
                "DELETE FROM strategy_structure_leg WHERE strategy_structure_id = %s",
                (strategy_structure_id,),
            )
            cur.execute(
                "DELETE FROM strategy_structure_meta WHERE strategy_structure_id = %s",
                (strategy_structure_id,),
            )
            _insert_legs(cur, strategy_structure_id, legs)
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
