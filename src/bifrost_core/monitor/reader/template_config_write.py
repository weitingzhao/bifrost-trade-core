"""Write strategy_dim and strategy_template (+ legs, params, characteristics)."""

import json
import logging
import re
from typing import Any, Dict, List, Optional

import psycopg2

from bifrost_core.monitor.reader import structure_type_config_constants as _const
from bifrost_core.monitor.reader import template_config
from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)

_DIM_TO_COL = {
    "direction": "dim_direction",
    "structure": "dim_structure",
    "coverage": "dim_coverage",
    "risk": "dim_risk",
    "volatility": "dim_volatility",
    "time": "dim_time",
}


def _conn_from_config(status_config: Optional[dict]) -> Any:
    if not status_config or (
        status_config.get("sink") != "postgres" and not status_config.get("postgres")
    ):
        return None
    try:
        params = _get_conn_params(status_config)
        return psycopg2.connect(**params)
    except Exception as e:
        logger.warning("template_config_write connect failed: %s", e)
        return None


def _validate_leg(leg: Dict[str, Any]) -> None:
    role = leg.get("role")
    if role is not None and str(role).strip() and str(role).strip() not in _const.LEG_ROLE_ALLOWED:
        raise ValueError(f"Invalid leg role: {role}")
    direction = leg.get("direction")
    if direction is not None and str(direction).strip() and str(direction).strip() not in _const.LEG_DIRECTION_ALLOWED:
        raise ValueError(f"Invalid leg direction: {direction}")
    opt = leg.get("option_right")
    if opt is not None:
        o = str(opt).strip()
        if o not in _const.LEG_OPTION_RIGHT_ALLOWED:
            raise ValueError(f"Invalid option_right: {opt}")


def _validate_dim_codes(conn: Any, payload: Dict[str, Any]) -> None:
    for dim, col in _DIM_TO_COL.items():
        code = payload.get(col)
        if code is None or str(code).strip() == "":
            continue
        c = str(code).strip()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM strategy_dim WHERE dim_type = %s AND code = %s",
                (dim, c),
            )
            if not cur.fetchone():
                raise ValueError(f"Invalid {dim} code: {c}")


def create_dim(status_config: Optional[dict], dim_type: str, payload: Dict[str, Any]) -> None:
    dt = (dim_type or "").strip()
    if dt not in _const.DIM_TYPE_ALLOWED:
        raise ValueError("Invalid dim_type")
    code = (payload.get("code") or "").strip()
    if not code or not re.match(r"^[a-z][a-z0-9_]*$", code):
        raise ValueError("code is required (lowercase snake_case)")
    label = (payload.get("display_label") or "").strip() or code
    so = int(payload["sort_order"]) if payload.get("sort_order") is not None else 0
    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_dim (dim_type, code, display_label, sort_order)
                VALUES (%s, %s, %s, %s)
                """,
                (dt, code, label, so),
            )
        conn.commit()
    except psycopg2.IntegrityError as e:
        conn.rollback()
        raise ValueError("Dimension code already exists for this type") from e
    finally:
        conn.close()


def update_dim(status_config: Optional[dict], strategy_dim_id: int, payload: Dict[str, Any]) -> bool:
    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT dim_type, code FROM strategy_dim WHERE strategy_dim_id = %s", (strategy_dim_id,))
            row = cur.fetchone()
            if not row:
                return False
            old_dt, old_code = row[0], row[1]
            sets = []
            vals: List[Any] = []
            if payload.get("display_label") is not None:
                sets.append("display_label = %s")
                vals.append((payload.get("display_label") or "").strip() or old_code)
            if payload.get("sort_order") is not None:
                sets.append("sort_order = %s")
                vals.append(int(payload["sort_order"]))
            new_code = payload.get("code")
            if new_code is not None:
                nc = str(new_code).strip()
                if nc and nc != old_code:
                    if not re.match(r"^[a-z][a-z0-9_]*$", nc):
                        raise ValueError("Invalid code")
                    col = _DIM_TO_COL.get(old_dt)
                    if col:
                        cur.execute(
                            f"SELECT COUNT(*) FROM strategy_template WHERE {col} = %s",
                            (old_code,),
                        )
                        if int(cur.fetchone()[0]) > 0:
                            raise ValueError("Cannot rename code: templates still reference it")
                    sets.append("code = %s")
                    vals.append(nc)
            if not sets:
                return True
            vals.append(strategy_dim_id)
            cur.execute(
                f"UPDATE strategy_dim SET {', '.join(sets)} WHERE strategy_dim_id = %s",
                vals,
            )
        conn.commit()
        return True
    except ValueError:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def delete_dim(status_config: Optional[dict], strategy_dim_id: int) -> None:
    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT dim_type, code FROM strategy_dim WHERE strategy_dim_id = %s", (strategy_dim_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("Dimension not found")
            dt, code = row[0], row[1]
            col = _DIM_TO_COL.get(dt)
            if col:
                cur.execute(f"SELECT 1 FROM strategy_template WHERE {col} = %s LIMIT 1", (code,))
                if cur.fetchone():
                    raise ValueError("Cannot delete: a template references this value")
            cur.execute("DELETE FROM strategy_dim WHERE strategy_dim_id = %s", (strategy_dim_id,))
        conn.commit()
    finally:
        conn.close()


def create_template(status_config: Optional[dict], payload: Dict[str, Any]) -> int:
    tc = (payload.get("template_code") or "").strip()
    if not tc or not re.match(r"^[a-z][a-z0-9_]*$", tc):
        raise ValueError("template_code is required (lowercase snake_case)")
    dn = (payload.get("display_name") or "").strip() or tc
    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    _validate_dim_codes(conn, payload)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_template (
                    template_code, display_name, dim_direction, dim_structure, dim_coverage,
                    dim_risk, dim_volatility, dim_time, explanation, typical_use, example, nature,
                    sort_order, is_active
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING strategy_template_id
                """,
                (
                    tc,
                    dn,
                    payload.get("dim_direction"),
                    payload.get("dim_structure"),
                    payload.get("dim_coverage"),
                    payload.get("dim_risk"),
                    payload.get("dim_volatility"),
                    payload.get("dim_time"),
                    (payload.get("explanation") or "").strip() or None,
                    (payload.get("typical_use") or "").strip() or None,
                    (payload.get("example") or "").strip() or None,
                    (payload.get("nature") or "").strip() or None,
                    int(payload["sort_order"]) if payload.get("sort_order") is not None else 0,
                    bool(payload.get("is_active", True)),
                ),
            )
            tid = int(cur.fetchone()[0])
        conn.commit()
        return tid
    except psycopg2.IntegrityError as e:
        conn.rollback()
        raise ValueError("template_code already exists") from e
    finally:
        conn.close()


def _normalize_template_code(raw: Any) -> str:
    s = (raw or "").strip().lower().replace(" ", "_")
    return s


def update_template(status_config: Optional[dict], strategy_template_id: int, payload: Dict[str, Any]) -> bool:
    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    _validate_dim_codes(conn, payload)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT template_code FROM strategy_template WHERE strategy_template_id = %s",
                (strategy_template_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            current_code = row[0]
            fields = []
            vals: List[Any] = []
            if "template_code" in payload:
                tc = _normalize_template_code(payload.get("template_code"))
                if not tc or not re.match(r"^[a-z][a-z0-9_]*$", tc):
                    raise ValueError("template_code must be lowercase snake_case")
                if tc != current_code:
                    fields.append("template_code = %s")
                    vals.append(tc)
            for key in (
                "display_name",
                "dim_direction",
                "dim_structure",
                "dim_coverage",
                "dim_risk",
                "dim_volatility",
                "dim_time",
                "explanation",
                "typical_use",
                "example",
                "nature",
                "sort_order",
                "is_active",
            ):
                if key in payload:
                    fields.append(f"{key} = %s")
                    v = payload[key]
                    if key in ("explanation", "typical_use", "example", "nature") and v is not None:
                        v = str(v).strip() or None
                    vals.append(v)
            if not fields:
                return True
            vals.append(strategy_template_id)
            cur.execute(
                f"UPDATE strategy_template SET {', '.join(fields)}, updated_at = now() "
                f"WHERE strategy_template_id = %s",
                vals,
            )
        conn.commit()
        return True
    except ValueError:
        conn.rollback()
        raise
    except psycopg2.IntegrityError as e:
        conn.rollback()
        raise ValueError("template_code already exists") from e
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def delete_template(status_config: Optional[dict], strategy_template_id: int) -> None:
    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    n = template_config.count_structures_using_template(conn, strategy_template_id)
    if n > 0:
        raise ValueError("Template is referenced by strategy structures")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM strategy_template WHERE strategy_template_id = %s",
                (strategy_template_id,),
            )
        conn.commit()
    finally:
        conn.close()


def replace_template_legs(
    status_config: Optional[dict], strategy_template_id: int, legs: List[Dict[str, Any]]
) -> None:
    if not isinstance(legs, list):
        raise ValueError("legs must be an array")
    for leg in legs:
        if isinstance(leg, dict):
            _validate_leg(leg)
    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM strategy_template WHERE strategy_template_id = %s",
                (strategy_template_id,),
            )
            if not cur.fetchone():
                raise ValueError("Template not found")
            cur.execute("DELETE FROM strategy_template_leg WHERE strategy_template_id = %s", (strategy_template_id,))
            for i, leg in enumerate(legs):
                if not isinstance(leg, dict):
                    continue
                qty = int(leg.get("quantity_default") or leg.get("quantity") or 1)
                cur.execute(
                    """
                    INSERT INTO strategy_template_leg
                        (strategy_template_id, sort_order, role, direction, option_right, quantity_default)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        strategy_template_id,
                        i,
                        leg.get("role"),
                        leg.get("direction"),
                        leg.get("option_right"),
                        qty,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def replace_template_params(
    status_config: Optional[dict], strategy_template_id: int, items: List[Dict[str, Any]]
) -> None:
    if not isinstance(items, list):
        raise ValueError("items must be an array")
    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        params_out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            mk = (it.get("meta_key") or "").strip()
            if not mk:
                continue
            pk = (it.get("param_kind") or "fixed").strip()
            if pk not in _const.PARAM_KIND_ALLOWED:
                raise ValueError(f"Invalid param_kind: {pk}")
            params_out.append(
                {
                    "meta_key": mk,
                    "display_label": (it.get("display_label") or "").strip() or None,
                    "default_value_text": it.get("default_value_text"),
                    "param_kind": pk,
                    "sort_order": int(it.get("sort_order") or 0),
                }
            )
        params_out.sort(key=lambda p: (p.get("sort_order") or 0, p.get("meta_key") or ""))
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE strategy_template
                SET params_json = %s::jsonb, updated_at = now()
                WHERE strategy_template_id = %s
                """,
                (json.dumps(params_out), strategy_template_id),
            )
            if cur.rowcount == 0:
                raise ValueError("Template not found")
        conn.commit()
    finally:
        conn.close()


def replace_template_characteristics(
    status_config: Optional[dict], strategy_template_id: int, lines: List[str]
) -> None:
    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        chars_out = [(text or "").strip() for text in (lines or []) if (text or "").strip()]
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE strategy_template
                SET characteristics_json = %s::jsonb, updated_at = now()
                WHERE strategy_template_id = %s
                """,
                (json.dumps(chars_out), strategy_template_id),
            )
            if cur.rowcount == 0:
                raise ValueError("Template not found")
        conn.commit()
    finally:
        conn.close()
