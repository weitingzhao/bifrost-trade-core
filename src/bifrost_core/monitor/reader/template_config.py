"""Read strategy dimensions (catalog) and strategy_template (+ legs_json, params, characteristics)."""

import json
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader import strategy_dim_catalog


def list_dims_grouped(conn: Any) -> Dict[str, List[Dict[str, Any]]]:
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.strategy_dim')")
            if cur.fetchone()[0] is not None:
                out: Dict[str, List[Dict[str, Any]]] = {}
                cur.execute(
                    """
                    SELECT dim_type, code, display_label, sort_order, strategy_dim_id
                    FROM strategy_dim
                    ORDER BY dim_type, sort_order, code
                    """
                )
                for r in cur.fetchall():
                    dt = r["dim_type"]
                    out.setdefault(dt, []).append(dict(r))
                return out
    except Exception:
        pass
    return strategy_dim_catalog.list_dims_grouped()


def list_dims_by_type(conn: Any, dim_type: str) -> List[Dict[str, Any]]:
    key = (dim_type or "").strip()
    if not key:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT to_regclass('public.strategy_dim')")
            if cur.fetchone()[0] is not None:
                cur.execute(
                    """
                    SELECT strategy_dim_id, dim_type, code, display_label, sort_order
                    FROM strategy_dim WHERE dim_type = %s
                    ORDER BY sort_order, code
                    """,
                    (key,),
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception:
        pass
    return strategy_dim_catalog.list_dims_by_type(key)


def _parse_json_field(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw


def _legs_from_json(raw: Any) -> List[Dict[str, Any]]:
    legs = _parse_json_field(raw, [])
    if not isinstance(legs, list):
        return []
    out: List[Dict[str, Any]] = []
    for leg in legs:
        if not isinstance(leg, dict):
            continue
        qty = leg.get("quantity_default") or leg.get("quantity") or 1
        out.append(
            {
                "role": leg.get("role"),
                "direction": leg.get("direction"),
                "option_right": leg.get("option_right"),
                "quantity": int(qty) if qty is not None else 1,
                "strike": leg.get("strike"),
                "expiration": leg.get("expiration") or "",
            }
        )
    return out


def get_template_row(conn: Any, strategy_template_id: int) -> Optional[Dict[str, Any]]:
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT strategy_template_id, template_code, display_name,
                       dim_direction, dim_structure, dim_coverage, dim_risk, dim_volatility, dim_time,
                       explanation, typical_use, example, nature, sort_order, is_active,
                       created_at, updated_at
                FROM strategy_template WHERE strategy_template_id = %s
                """,
                (strategy_template_id,),
            )
            r = cur.fetchone()
            return dict(r) if r else None
    except Exception:
        return None


def get_template_by_code(conn: Any, template_code: str) -> Optional[Dict[str, Any]]:
    key = (template_code or "").strip()
    if not key:
        return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT strategy_template_id, template_code, display_name,
                       dim_direction, dim_structure, dim_coverage, dim_risk, dim_volatility, dim_time,
                       explanation, typical_use, example, nature, sort_order, is_active,
                       created_at, updated_at
                FROM strategy_template WHERE template_code = %s
                """,
                (key,),
            )
            r = cur.fetchone()
            return dict(r) if r else None
    except Exception:
        return None


def get_template_legs(conn: Any, strategy_template_id: int) -> List[Dict[str, Any]]:
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT legs_json FROM strategy_template WHERE strategy_template_id = %s",
                (strategy_template_id,),
            )
            row = cur.fetchone()
        if row and row.get("legs_json") is not None:
            return _legs_from_json(row["legs_json"])
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT role, direction, option_right, quantity_default, sort_order
                FROM strategy_template_leg
                WHERE strategy_template_id = %s ORDER BY sort_order
                """,
                (strategy_template_id,),
            )
            rows = cur.fetchall()
        return _legs_from_json(
            [
                {
                    "role": r.get("role"),
                    "direction": r.get("direction"),
                    "option_right": r.get("option_right"),
                    "quantity_default": r.get("quantity_default"),
                }
                for r in rows
            ]
        )
    except Exception:
        return []


def list_templates(conn: Any, active_only: bool = True) -> List[Dict[str, Any]]:
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            wh = "WHERE is_active = true" if active_only else ""
            cur.execute(
                f"""
                SELECT strategy_template_id, template_code, display_name,
                       dim_direction, dim_structure, dim_coverage, dim_risk, dim_volatility, dim_time,
                       explanation, typical_use, example, nature, sort_order, is_active,
                       created_at, updated_at
                FROM strategy_template {wh}
                ORDER BY sort_order, display_name
                """
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def get_template_detail(conn: Any, strategy_template_id: int) -> Optional[Dict[str, Any]]:
    row = get_template_row(conn, strategy_template_id)
    if not row:
        return None
    row["legs"] = get_template_legs(conn, strategy_template_id)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT params_json, characteristics_json
                FROM strategy_template
                WHERE strategy_template_id = %s
                """,
                (strategy_template_id,),
            )
            jrow = cur.fetchone() or {}
        params = _parse_json_field(jrow.get("params_json"), [])
        if not isinstance(params, list):
            params = []
        row["meta_params"] = [dict(p) for p in params if isinstance(p, dict)]
        chars = _parse_json_field(jrow.get("characteristics_json"), [])
        if not isinstance(chars, list):
            chars = []
        row["characteristics"] = [str(c) for c in chars if c is not None and str(c).strip()]
    except Exception:
        row["meta_params"] = []
        row["characteristics"] = []
    return row


def count_structures_using_template(conn: Any, strategy_template_id: int) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM strategy_structure WHERE strategy_template_id = %s",
                (strategy_template_id,),
            )
            return int(cur.fetchone()[0])
    except Exception:
        return 0
