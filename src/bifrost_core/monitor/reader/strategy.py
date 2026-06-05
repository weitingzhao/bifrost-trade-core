"""Strategy structure and strategy_history readers. Used by StatusReader and API."""

from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor


def get_structure_by_id(conn: Any, strategy_structure_id: int) -> Optional[Dict[str, Any]]:
    """Return one strategy_structure as dict with legs, constraints, metadata assembled from child tables."""
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
                       s.version, s.is_active, s.created_at, s.updated_at, s.notes
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

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT role, direction, option_right, quantity, strike, expiration
                FROM strategy_structure_leg
                WHERE strategy_structure_id = %s ORDER BY sort_order
                """,
                (strategy_structure_id,),
            )
            legs_rows = cur.fetchall()
        legs = [
            {
                "role": r.get("role"),
                "direction": r.get("direction"),
                "option_right": r.get("option_right"),
                "quantity": r.get("quantity"),
                "strike": r.get("strike"),
                "expiration": r.get("expiration"),
            }
            for r in legs_rows
        ]
        out["legs"] = legs

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT constraint_type, constraint_value_text, constraint_value_int
                FROM strategy_structure_constraint
                WHERE strategy_structure_id = %s
                """,
                (strategy_structure_id,),
            )
            constraint_rows = cur.fetchall()
        constraints = [
            {
                "constraint_type": r.get("constraint_type"),
                "constraint_value_text": r.get("constraint_value_text"),
                "constraint_value_int": r.get("constraint_value_int"),
            }
            for r in constraint_rows
        ]
        out["constraints"] = constraints

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT meta_key, meta_value_text
                FROM strategy_structure_meta
                WHERE strategy_structure_id = %s
                """,
                (strategy_structure_id,),
            )
            meta_rows = cur.fetchall()
        metadata = {r["meta_key"]: r.get("meta_value_text") for r in meta_rows if r.get("meta_key")}
        out["metadata"] = metadata

        return out
    except Exception:
        return None


def list_structures(conn: Any, active_only: bool = True) -> List[Dict[str, Any]]:
    """Return list of strategy_structure rows with legs and constraints for sheet summarization."""
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
                           s.version, s.is_active, s.created_at, s.updated_at, s.notes
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
                           s.version, s.is_active, s.created_at, s.updated_at, s.notes
                    FROM strategy_structure s
                    LEFT JOIN strategy_template t ON t.strategy_template_id = s.strategy_template_id
                    ORDER BY s.name
                    """
                )
            rows = cur.fetchall()
        out = [dict(r) for r in rows]
        ids = [r["strategy_structure_id"] for r in out]
        if not ids:
            return out

        legs_by_id: Dict[int, List[Dict[str, Any]]] = {i: [] for i in ids}
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT strategy_structure_id, role, direction, option_right, quantity, strike, expiration
                FROM strategy_structure_leg
                WHERE strategy_structure_id = ANY(%s) ORDER BY strategy_structure_id, sort_order
                """,
                (ids,),
            )
            for r in cur.fetchall():
                sid = r["strategy_structure_id"]
                legs_by_id.setdefault(sid, []).append(
                    {
                        "role": r.get("role"),
                        "direction": r.get("direction"),
                        "option_right": r.get("option_right"),
                        "quantity": r.get("quantity"),
                        "strike": r.get("strike"),
                        "expiration": r.get("expiration"),
                    }
                )

        constraints_by_id: Dict[int, List[Dict[str, Any]]] = {i: [] for i in ids}
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT strategy_structure_id, constraint_type, constraint_value_text, constraint_value_int
                FROM strategy_structure_constraint
                WHERE strategy_structure_id = ANY(%s)
                """,
                (ids,),
            )
            for r in cur.fetchall():
                sid = r["strategy_structure_id"]
                constraints_by_id.setdefault(sid, []).append(
                    {
                        "constraint_type": r.get("constraint_type"),
                        "constraint_value_text": r.get("constraint_value_text"),
                        "constraint_value_int": r.get("constraint_value_int"),
                    }
                )

        for item in out:
            item["legs"] = legs_by_id.get(item["strategy_structure_id"], [])
            item["constraints"] = constraints_by_id.get(item["strategy_structure_id"], [])
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
                           (SELECT array_agg(symbol ORDER BY sort_order)
                            FROM strategy_opportunity_symbol
                            WHERE strategy_opportunity_id = o.strategy_opportunity_id) AS symbols
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
                           (SELECT array_agg(symbol ORDER BY sort_order)
                            FROM strategy_opportunity_symbol
                            WHERE strategy_opportunity_id = o.strategy_opportunity_id) AS symbols
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
    """Return one strategy_opportunity with structure_name, gate_safety_name, symbols and entry_conditions from child tables."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT o.strategy_opportunity_id, o.name, o.strategy_structure_id,
                       o.default_gate_safety_strategy_id, o.scope_type,
                       o.is_active, o.created_at, o.updated_at,
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

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT symbol FROM strategy_opportunity_symbol WHERE strategy_opportunity_id = %s ORDER BY sort_order",
                (strategy_opportunity_id,),
            )
            symbol_rows = cur.fetchall()
        symbols = [r["symbol"] for r in symbol_rows] if symbol_rows else []

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT condition_type, value_text, value_numeric
                FROM strategy_opportunity_entry_condition
                WHERE strategy_opportunity_id = %s ORDER BY sort_order
                """,
                (strategy_opportunity_id,),
            )
            cond_rows = cur.fetchall()
        entry_conditions = [
            {
                "condition_type": r.get("condition_type"),
                "value_text": r.get("value_text"),
                "value_numeric": float(r["value_numeric"]) if r.get("value_numeric") is not None else None,
            }
            for r in cond_rows
        ]

        out["symbols"] = symbols
        out["entry_conditions"] = entry_conditions
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


def get_strategy_history(
    conn: Any,
    from_ts: Optional[float] = None,
    to_ts: Optional[float] = None,
    strategy_structure_id: Optional[int] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Query strategy_history with optional time range and structure filter.
    Returns list of rows (strategy_history_id, strategy_structure_id, ts, state_summary, created_at).
    """
    limit = min(max(1, limit), 500)
    conditions: List[str] = []
    params: List[Any] = []
    if from_ts is not None:
        conditions.append("h.ts >= to_timestamp(%s)")
        params.append(from_ts)
    if to_ts is not None:
        conditions.append("h.ts <= to_timestamp(%s)")
        params.append(to_ts)
    if strategy_structure_id is not None:
        conditions.append("h.strategy_structure_id = %s")
        params.append(strategy_structure_id)
    where = " AND ".join(conditions) if conditions else "TRUE"
    params.append(limit)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT strategy_history_id, strategy_structure_id, ts, state_summary, created_at
                FROM strategy_history h
                WHERE {where}
                ORDER BY h.ts DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
