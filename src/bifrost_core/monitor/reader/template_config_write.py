"""Write strategy_template (+ legs, params, characteristics).

Strategy dimensions are catalog-defined (strategy_dim_catalog, the dim_*_t enums) and are not written here.

``patch_template`` and ``delete_template_strict`` (core 0.33.0, TD-15) raise the
``Write*`` outcomes; the older writers keep raising ``ValueError`` for one release.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

import psycopg2

from bifrost_core.monitor.reader import strategy_dim_catalog
from bifrost_core.monitor.reader import structure_type_config_constants as _const
from bifrost_core.monitor.reader import template_config
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteConflict, WriteFailed, WriteInvalid, WriteNotFound
from bifrost_core.monitor.schemas.gate_params import TemplateLeg
from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)


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
    strategy_dim_catalog.validate_dim_fields(payload)


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
    legs_out: List[Dict[str, Any]] = []
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            continue
        _validate_leg(leg)
        qty = int(leg.get("quantity_default") or leg.get("quantity") or 1)
        legs_out.append(
            {
                "role": leg.get("role"),
                "direction": leg.get("direction"),
                "option_right": leg.get("option_right"),
                "quantity": qty,
                "quantity_default": qty,
                "sort_order": i,
            }
        )
    validated = [TemplateLeg.model_validate(leg).model_dump() for leg in legs_out]
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
            cur.execute(
                """
                UPDATE strategy_template
                SET legs_json = %s::jsonb, updated_at = now()
                WHERE strategy_template_id = %s
                """,
                (json.dumps(validated), strategy_template_id),
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


# --- TD-15 writers (core 0.33.0): return the row / raise Write* ---------------------

_DIM_FIELDS = ("dim_direction", "dim_structure", "dim_coverage", "dim_risk", "dim_volatility", "dim_time")
_TEMPLATE_TEXT_FIELDS = ("explanation", "typical_use", "example", "nature")
TEMPLATE_PATCHABLE = (
    "template_code",
    "display_name",
    *_DIM_FIELDS,
    *_TEMPLATE_TEXT_FIELDS,
    "sort_order",
    "is_active",
)
_TEMPLATE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def patch_template(conn_or_config: Any, strategy_template_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change the template's own fields; return it as ``template_config.get_template_detail`` reads it.

    ``template_code`` NOT NULL, unique, already lowercase snake_case (not rewritten) ·
    ``display_name`` NOT NULL text · ``dim_*`` nullable catalog codes · ``explanation`` /
    ``typical_use`` / ``example`` / ``nature`` nullable text · ``sort_order`` NOT NULL whole
    number · ``is_active`` boolean. Legs, params and characteristics keep their own
    replace writers. Raises WriteInvalid, WriteNotFound, WriteConflict (code in use), WriteFailed.
    """
    what = f"template {strategy_template_id}"
    fields = ws.check_fields(fields, TEMPLATE_PATCHABLE, "template")
    columns: Dict[str, Any] = {}
    if "template_code" in fields:
        code = ws.text(fields["template_code"], "template_code", nullable=False)
        if not _TEMPLATE_CODE_RE.match(code or ""):
            raise WriteInvalid("template_code must be lowercase snake_case (a-z, 0-9, _; a letter first).")
        columns["template_code"] = code
    if "display_name" in fields:
        columns["display_name"] = ws.text(fields["display_name"], "display_name", nullable=False)
    for name in _DIM_FIELDS:
        if name in fields:
            code = ws.text(fields[name], name, nullable=True)
            if code is not None and not strategy_dim_catalog.is_valid_dim_code(name[len("dim_"):], code):
                raise WriteInvalid(f"{name}: {code} is not a code in the dim catalog.")
            columns[name] = code
    for name in _TEMPLATE_TEXT_FIELDS:
        if name in fields:
            columns[name] = ws.text(fields[name], name, nullable=True)
    if "sort_order" in fields:
        columns["sort_order"] = ws.integer(fields["sort_order"], "sort_order", nullable=False)
    if "is_active" in fields:
        columns["is_active"] = ws.boolean(fields["is_active"], "is_active")
    assignments, values = ws.set_clause(columns)
    with ws.write_connection(conn_or_config, what) as conn:
        try:
            with ws.write_transaction(conn, what):
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE strategy_template SET {assignments} WHERE strategy_template_id = %s",
                        [*values, strategy_template_id],
                    )
                    if cur.rowcount == 0:
                        raise WriteNotFound(f"No template {strategy_template_id}.")
                row = template_config.get_template_detail(conn, strategy_template_id)
                if row is None:
                    raise WriteFailed(f"{what} was changed but could not be read back; nothing was saved.")
        except WriteConflict as e:
            if "template_code" in columns:
                raise WriteConflict(
                    f"template_code {columns['template_code']} is already used by another template."
                ) from e
            raise
    return row


def delete_template_strict(conn_or_config: Any, strategy_template_id: int) -> Dict[str, Any]:
    """Hard-delete a template no structure uses. Returns ``{"deleted": "hard", "strategy_template_id"}``.

    A structure that uses it -- active or deactivated (a deactivated structure still
    references the template) -- refuses the delete with WriteConflict naming them.
    Raises WriteNotFound, WriteConflict, WriteFailed.
    """
    what = f"template {strategy_template_id}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what, on_fk="conflict"):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM strategy_template WHERE strategy_template_id = %s FOR UPDATE",
                (strategy_template_id,),
            )
            if cur.fetchone() is None:
                raise WriteNotFound(f"No template {strategy_template_id}.")
            cur.execute(
                "SELECT name, is_active FROM strategy_structure WHERE strategy_template_id = %s "
                "ORDER BY is_active DESC, name",
                (strategy_template_id,),
            )
            users = cur.fetchall() or []
            if users:
                names = [f"{u[0]}{'' if u[1] else ' (deactivated)'}" for u in users]
                verb = "uses" if len(users) == 1 else "use"
                raise WriteConflict(
                    f"{ws.plural(len(users), 'structure', 'structures')} {verb} this template: "
                    f"{ws.name_list(names)}. Point {'it' if len(users) == 1 else 'them'} at another template first."
                )
            cur.execute("DELETE FROM strategy_template WHERE strategy_template_id = %s", (strategy_template_id,))
            if cur.rowcount == 0:
                raise WriteNotFound(f"No template {strategy_template_id}.")
    return {"deleted": "hard", "strategy_template_id": strategy_template_id}
