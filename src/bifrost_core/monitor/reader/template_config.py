"""Read strategy_template (+ legs_json, params, characteristics).

Strategy dimensions are not read here: they come from strategy_dim_catalog.
"""

import json
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor


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
    """The template's legs from strategy_template.legs_json (NOT NULL, default '[]')."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT legs_json FROM strategy_template WHERE strategy_template_id = %s",
                (strategy_template_id,),
            )
            row = cur.fetchone()
        return _legs_from_json(row["legs_json"]) if row else []
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
