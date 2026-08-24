"""Strategy structure readers. Used by StatusReader and API."""

import json
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor


def _parse_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _structure_legs_from_row(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    legs = _parse_json(row.get("legs_json"), [])
    if isinstance(legs, list) and legs:
        return [
            {
                "role": leg.get("role"),
                "direction": leg.get("direction"),
                "option_right": leg.get("option_right"),
                "quantity": leg.get("quantity"),
                "strike": leg.get("strike"),
                "expiration": leg.get("expiration"),
            }
            for leg in legs
            if isinstance(leg, dict)
        ]
    return []


def get_structure_by_id(conn: Any, strategy_structure_id: int) -> Optional[Dict[str, Any]]:
    """Return one strategy_structure as dict with legs and metadata from jsonb columns."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT s.strategy_structure_id, s.name,
                       t.template_code AS structure_type,
                       CAST(NULL AS text) AS structure_subtype,
                       s.strategy_template_id,
                       t.dim_direction, t.dim_structure, t.dim_coverage,
                       t.dim_risk, t.dim_volatility, t.dim_time,
                       t.template_code AS template_code, t.display_name AS template_display_name,
                       s.version, s.is_active, s.created_at, s.updated_at, s.notes,
                       s.legs_json, s.meta_json
                FROM strategy_structure s
                LEFT JOIN strategy_template t ON t.strategy_template_id = s.strategy_template_id
                WHERE s.strategy_structure_id = %s
                """,
                (strategy_structure_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        out = dict(row)
        out["legs"] = _structure_legs_from_row(out)
        raw_meta = _parse_json(out.pop("meta_json", None), {})
        out.pop("legs_json", None)
        if isinstance(raw_meta, dict):
            out["metadata"] = {str(k): v for k, v in raw_meta.items() if k}
        else:
            out["metadata"] = {}
        return out
    except Exception:
        return None


def list_structures(conn: Any, active_only: bool = True) -> List[Dict[str, Any]]:
    """Return list of strategy_structure rows with legs for sheet summarization."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if active_only:
                cur.execute(
                    """
                    SELECT s.strategy_structure_id, s.name,
                           t.template_code AS structure_type,
                           CAST(NULL AS text) AS structure_subtype,
                           t.display_name AS structure_subtype_label,
                           s.strategy_template_id,
                           t.dim_direction, t.dim_structure, t.dim_coverage,
                           t.dim_risk, t.dim_volatility, t.dim_time,
                           t.template_code AS template_code, t.display_name AS template_display_name,
                           s.version, s.is_active, s.created_at, s.updated_at, s.notes,
                           s.legs_json
                    FROM strategy_structure s
                    LEFT JOIN strategy_template t ON t.strategy_template_id = s.strategy_template_id
                    WHERE s.is_active = true
                    ORDER BY s.name
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT s.strategy_structure_id, s.name,
                           t.template_code AS structure_type,
                           CAST(NULL AS text) AS structure_subtype,
                           t.display_name AS structure_subtype_label,
                           s.strategy_template_id,
                           t.dim_direction, t.dim_structure, t.dim_coverage,
                           t.dim_risk, t.dim_volatility, t.dim_time,
                           t.template_code AS template_code, t.display_name AS template_display_name,
                           s.version, s.is_active, s.created_at, s.updated_at, s.notes,
                           s.legs_json
                    FROM strategy_structure s
                    LEFT JOIN strategy_template t ON t.strategy_template_id = s.strategy_template_id
                    ORDER BY s.name
                    """
                )
            rows = cur.fetchall()
        out = [dict(r) for r in rows]
        for item in out:
            item["legs"] = _structure_legs_from_row(item)
            item.pop("legs_json", None)
        return out
    except Exception:
        return []


def list_opportunities(conn: Any, active_only: bool = True) -> List[Dict[str, Any]]:
    """Return list of strategy_opportunity rows with structure_name, gate_safety_name, scope_type, and symbols for list UI."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if active_only:
                cur.execute(
                    """
                    SELECT o.strategy_opportunity_id, o.name, o.strategy_structure_id,
                           o.default_gate_safety_strategy_id, o.scope_type,
                           o.is_active, o.created_at, o.updated_at,
                           s.name AS structure_name,
                           g.name AS gate_safety_name,
                           (SELECT array_agg(sym ORDER BY ord)
                            FROM jsonb_array_elements_text(o.symbols_json) WITH ORDINALITY AS t(sym, ord)) AS symbols
                    FROM strategy_opportunity o
                    LEFT JOIN strategy_structure s ON s.strategy_structure_id = o.strategy_structure_id
                    LEFT JOIN gate_safety_strategy g ON g.gate_safety_strategy_id = o.default_gate_safety_strategy_id
                    WHERE o.is_active = true
                    ORDER BY o.name
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT o.strategy_opportunity_id, o.name, o.strategy_structure_id,
                           o.default_gate_safety_strategy_id, o.scope_type,
                           o.is_active, o.created_at, o.updated_at,
                           s.name AS structure_name,
                           g.name AS gate_safety_name,
                           (SELECT array_agg(sym ORDER BY ord)
                            FROM jsonb_array_elements_text(o.symbols_json) WITH ORDINALITY AS t(sym, ord)) AS symbols
                    FROM strategy_opportunity o
                    LEFT JOIN strategy_structure s ON s.strategy_structure_id = o.strategy_structure_id
                    LEFT JOIN gate_safety_strategy g ON g.gate_safety_strategy_id = o.default_gate_safety_strategy_id
                    ORDER BY o.name
                    """
                )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_opportunity_by_id(conn: Any, strategy_opportunity_id: int) -> Optional[Dict[str, Any]]:
    """Return one strategy_opportunity with symbols and entry_conditions from jsonb columns."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT o.strategy_opportunity_id, o.name, o.strategy_structure_id,
                       o.default_gate_safety_strategy_id, o.scope_type,
                       o.is_active, o.created_at, o.updated_at,
                       o.symbols_json, o.entry_conditions_json,
                       s.name AS structure_name,
                       g.name AS gate_safety_name
                FROM strategy_opportunity o
                LEFT JOIN strategy_structure s ON s.strategy_structure_id = o.strategy_structure_id
                LEFT JOIN gate_safety_strategy g ON g.gate_safety_strategy_id = o.default_gate_safety_strategy_id
                WHERE o.strategy_opportunity_id = %s
                """,
                (strategy_opportunity_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        out = dict(row)
        symbols = _parse_json(out.pop("symbols_json", None), [])
        if not isinstance(symbols, list):
            symbols = []
        out["symbols"] = [str(s) for s in symbols if s]

        conds = _parse_json(out.pop("entry_conditions_json", None), [])
        if not isinstance(conds, list):
            conds = []
        out["entry_conditions"] = [
            {
                "condition_type": c.get("condition_type"),
                "value_text": c.get("value_text"),
                "value_numeric": float(c["value_numeric"]) if c.get("value_numeric") is not None else None,
            }
            for c in conds
            if isinstance(c, dict)
        ]
        return out
    except Exception:
        return None


def _allocation_row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble strategy_opportunity_ids (list), allocation_limits (dict), and top-level max_positions/max_bp_pct for API shape."""
    out = dict(row)
    ids = row.get("strategy_opportunity_ids")
    out["strategy_opportunity_ids"] = list(ids) if ids is not None else []
    limits = {}
    if row.get("max_positions") is not None:
        limits["max_positions"] = int(row["max_positions"])
    if row.get("max_bp_pct") is not None:
        limits["max_bp_pct"] = float(row["max_bp_pct"])
    out["allocation_limits"] = limits if limits else None
    out["max_positions"] = row.get("max_positions")
    out["max_bp_pct"] = float(row["max_bp_pct"]) if row.get("max_bp_pct") is not None else None
    return out


def list_allocations(conn: Any, active_only: bool = True) -> List[Dict[str, Any]]:
    """Return list of strategy_allocation rows with gate_safety_name and opportunity ids from junction table."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if active_only:
                cur.execute(
                    """
                    SELECT p.strategy_allocation_id, p.name, p.gate_safety_strategy_id,
                           p.max_positions, p.max_bp_pct, p.is_active,
                           p.created_at, p.updated_at, g.name AS gate_safety_name,
                           (SELECT array_agg(po.strategy_opportunity_id ORDER BY po.sort_order)
                            FROM strategy_allocation_opportunity po
                            WHERE po.strategy_allocation_id = p.strategy_allocation_id) AS strategy_opportunity_ids
                    FROM strategy_allocation p
                    LEFT JOIN gate_safety_strategy g ON g.gate_safety_strategy_id = p.gate_safety_strategy_id
                    WHERE p.is_active = true
                    ORDER BY p.name
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT p.strategy_allocation_id, p.name, p.gate_safety_strategy_id,
                           p.max_positions, p.max_bp_pct, p.is_active,
                           p.created_at, p.updated_at, g.name AS gate_safety_name,
                           (SELECT array_agg(po.strategy_opportunity_id ORDER BY po.sort_order)
                            FROM strategy_allocation_opportunity po
                            WHERE po.strategy_allocation_id = p.strategy_allocation_id) AS strategy_opportunity_ids
                    FROM strategy_allocation p
                    LEFT JOIN gate_safety_strategy g ON g.gate_safety_strategy_id = p.gate_safety_strategy_id
                    ORDER BY p.name
                    """
                )
            rows = cur.fetchall()
        return [_allocation_row_to_dict(dict(r)) for r in rows]
    except Exception:
        return []


def get_allocation_by_id(conn: Any, strategy_allocation_id: int) -> Optional[Dict[str, Any]]:
    """Return one strategy_allocation row by id with gate_safety_name and opportunity ids from junction table."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT p.strategy_allocation_id, p.name, p.gate_safety_strategy_id,
                       p.max_positions, p.max_bp_pct, p.is_active,
                       p.created_at, p.updated_at, g.name AS gate_safety_name,
                       (SELECT array_agg(po.strategy_opportunity_id ORDER BY po.sort_order)
                        FROM strategy_allocation_opportunity po
                        WHERE po.strategy_allocation_id = p.strategy_allocation_id) AS strategy_opportunity_ids
                FROM strategy_allocation p
                LEFT JOIN gate_safety_strategy g ON g.gate_safety_strategy_id = p.gate_safety_strategy_id
                WHERE p.strategy_allocation_id = %s
                """,
                (strategy_allocation_id,),
            )
            row = cur.fetchone()
        return _allocation_row_to_dict(dict(row)) if row else None
    except Exception:
        return None
