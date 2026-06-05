"""Read strategy_dim and strategy_template (+ legs, params, characteristics)."""

from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor


def list_dims_grouped(conn: Any) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT dim_type, code, display_label, sort_order, strategy_dim_id
                FROM strategy_dim
                ORDER BY dim_type, sort_order, code
                """
            )
            for r in cur.fetchall():
                dt = r["dim_type"]
                out.setdefault(dt, []).append(
                    {
                        "strategy_dim_id": r["strategy_dim_id"],
                        "dim_type": dt,
                        "code": r["code"],
                        "display_label": r["display_label"],
                        "sort_order": r["sort_order"],
                    }
                )
        return out
    except Exception:
        return out


def list_dims_by_type(conn: Any, dim_type: str) -> List[Dict[str, Any]]:
    key = (dim_type or "").strip()
    if not key:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
        return []


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
                """
                SELECT role, direction, option_right, quantity_default, sort_order
                FROM strategy_template_leg
                WHERE strategy_template_id = %s ORDER BY sort_order
                """,
                (strategy_template_id,),
            )
            rows = cur.fetchall()
        return [
            {
                "role": r.get("role"),
                "direction": r.get("direction"),
                "option_right": r.get("option_right"),
                "quantity": int(r["quantity_default"]) if r.get("quantity_default") is not None else 1,
                "strike": None,
                "expiration": "",
            }
            for r in rows
        ]
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
                SELECT meta_key, display_label, default_value_text, param_kind, sort_order
                FROM strategy_template_param
                WHERE strategy_template_id = %s ORDER BY sort_order, meta_key
                """,
                (strategy_template_id,),
            )
            row["meta_params"] = [dict(r) for r in cur.fetchall()]
            cur.execute(
                """
                SELECT characteristic_text, sort_order
                FROM strategy_template_characteristic
                WHERE strategy_template_id = %s ORDER BY sort_order
                """,
                (strategy_template_id,),
            )
            row["characteristics"] = [r["characteristic_text"] for r in cur.fetchall()]
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
