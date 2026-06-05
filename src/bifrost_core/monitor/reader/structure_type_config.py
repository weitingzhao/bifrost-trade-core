"""Read structure type config from strategy_structure_type and related tables.

Used by strategies API to drive Wizard Step 1/2 (types list, default legs, subtypes with
characteristics and meta params, infer_rules for Edit). All data from config tables; no JSON.
"""

from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor


def list_structure_types(conn: Any) -> List[Dict[str, Any]]:
    """Return all structure types from strategy_structure_type, ordered by sort_order."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT structure_type, display_label, sort_order, has_subtypes, type_explanation
                FROM strategy_structure_type
                ORDER BY sort_order
                """
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception:
        return []


def get_default_legs(conn: Any, structure_type: str) -> List[Dict[str, Any]]:
    """Return default legs for the given structure type from strategy_structure_type_leg.

    Returns list of leg dicts compatible with default-legs API shape: role, direction,
    option_right, quantity (from quantity_default), strike (null), expiration (empty).
    """
    if not (structure_type and isinstance(structure_type, str)):
        return []
    key = structure_type.strip()
    if not key:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT role, direction, option_right, quantity_default, sort_order
                FROM strategy_structure_type_leg
                WHERE structure_type = %s
                ORDER BY sort_order
                """,
                (key,),
            )
            rows = cur.fetchall()
        if not rows:
            return []
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


def get_subtype_legs_only(
    conn: Any, structure_type: str, subtype: Optional[str]
) -> Optional[List[Dict[str, Any]]]:
    """Return subtype-specific legs from strategy_structure_subtype_leg, or None if none (inherits type).

    Used by get_schema_from_db to decide whether to use subtype schema or type schema.
    """
    if not (structure_type and isinstance(structure_type, str)) or not (subtype and isinstance(subtype, str)):
        return None
    key_type = structure_type.strip()
    key_sub = subtype.strip()
    if not key_type or not key_sub:
        return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT role, direction, option_right, quantity_default, sort_order
                FROM strategy_structure_subtype_leg
                WHERE structure_type = %s AND subtype = %s
                ORDER BY sort_order
                """,
                (key_type, key_sub),
            )
            rows = cur.fetchall()
        if not rows:
            return None
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
        return None


def get_default_legs_for_subtype(
    conn: Any, structure_type: str, subtype: Optional[str]
) -> List[Dict[str, Any]]:
    """Return default legs for the given (structure_type, subtype).

    If subtype-specific legs exist in strategy_structure_subtype_leg, they are used.
    Otherwise, fall back to type-level strategy_structure_type_leg via get_default_legs.
    """
    if not (structure_type and isinstance(structure_type, str)):
        return []
    key_type = structure_type.strip()
    key_sub = (subtype or "").strip()
    if not key_type:
        return []
    if not key_sub:
        # No subtype specified: behave like get_default_legs.
        return get_default_legs(conn, key_type)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT role, direction, option_right, quantity_default, sort_order
                FROM strategy_structure_subtype_leg
                WHERE structure_type = %s AND subtype = %s
                ORDER BY sort_order
                """,
                (key_type, key_sub),
            )
            rows = cur.fetchall()
        if not rows:
            return get_default_legs(conn, key_type)
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
        # On error, fall back to type-level legs to avoid breaking callers.
        return get_default_legs(conn, key_type)


def get_subtypes_with_detail(conn: Any, structure_type: str) -> Dict[str, Any]:
    """Return subtypes with characteristics and meta_params, plus infer_rules for the given structure type.

    Response shape: { "subtypes": [...], "infer_rules": [...] }.
    Each subtype: subtype, display_label, example, typical_use, subtype_explanation, nature, sort_order,
    characteristics (list of strings), meta_params (list of { meta_key, display_label, default_value_text, param_kind, sort_order }).
    """
    if not (structure_type and isinstance(structure_type, str)):
        return {"subtypes": [], "infer_rules": []}
    key = structure_type.strip()
    if not key:
        return {"subtypes": [], "infer_rules": []}
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT structure_type, subtype, display_label, example, typical_use,
                       subtype_explanation, nature, sort_order
                FROM strategy_structure_subtype
                WHERE structure_type = %s
                ORDER BY sort_order
                """,
                (key,),
            )
            sub_rows = cur.fetchall()
        if not sub_rows:
            return {"subtypes": [], "infer_rules": []}

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT structure_type, subtype, sort_order, characteristic_text
                FROM strategy_structure_subtype_characteristic
                WHERE structure_type = %s
                ORDER BY subtype, sort_order
                """,
                (key,),
            )
            char_rows = cur.fetchall()
        chars_by_sub: Dict[str, List[str]] = {}
        for r in char_rows:
            sub = r.get("subtype") or ""
            text = r.get("characteristic_text") or ""
            if sub not in chars_by_sub:
                chars_by_sub[sub] = []
            chars_by_sub[sub].append(text)

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT structure_type, subtype, meta_key, display_label, default_value_text, param_kind, sort_order
                FROM strategy_structure_subtype_meta_param
                WHERE structure_type = %s
                ORDER BY subtype, sort_order
                """,
                (key,),
            )
            param_rows = cur.fetchall()
        params_by_sub: Dict[str, List[Dict[str, Any]]] = {}
        for r in param_rows:
            sub = r.get("subtype") or ""
            params_by_sub.setdefault(sub, []).append(
                {
                    "meta_key": r.get("meta_key"),
                    "display_label": r.get("display_label"),
                    "default_value_text": r.get("default_value_text"),
                    "param_kind": r.get("param_kind"),
                    "sort_order": r.get("sort_order"),
                }
            )

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT meta_key, meta_value_text, subtype
                FROM strategy_structure_subtype_rule
                WHERE structure_type = %s
                """,
                (key,),
            )
            rule_rows = cur.fetchall()
        infer_rules = [
            {
                "meta_key": r.get("meta_key"),
                "meta_value_text": r.get("meta_value_text"),
                "subtype": r.get("subtype"),
            }
            for r in rule_rows
        ]

        subtypes = []
        for r in sub_rows:
            sub = r.get("subtype") or ""
            subtypes.append(
                {
                    "subtype": sub,
                    "display_label": r.get("display_label"),
                    "example": r.get("example"),
                    "typical_use": r.get("typical_use"),
                    "subtype_explanation": r.get("subtype_explanation"),
                    "nature": r.get("nature"),
                    "sort_order": r.get("sort_order"),
                    "characteristics": chars_by_sub.get(sub, []),
                    "meta_params": params_by_sub.get(sub, []),
                }
            )

        return {"subtypes": subtypes, "infer_rules": infer_rules}
    except Exception:
        return {"subtypes": [], "infer_rules": []}
