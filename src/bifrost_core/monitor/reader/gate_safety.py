"""Gate safety: load gates from DB by gate_safety_strategy_id. Shape compatible with config['gates'] for get_hedge_config."""

from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor


def get_gates_by_id(conn: Any, gate_safety_strategy_id: int) -> Optional[Dict[str, Any]]:
    """Load gates from gate_safety_* tables and return a dict in the shape of config['gates'].
    So the caller can set config['gates'] = get_gates_by_id(conn, id) and get_hedge_config(config) will work.
    Returns None if the boundary set or any required child row is missing.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT gate_safety_strategy_id, name, version,
                       dim_direction, dim_structure, dim_coverage, dim_risk, dim_volatility, dim_time,
                       is_active,
                       min_dte, max_dte, atm_band_pct, blackout_days_before, blackout_days_after,
                       trading_hours_only
                FROM gate_safety_strategy WHERE gate_safety_strategy_id = %s
                """,
                (gate_safety_strategy_id,),
            )
            root = cur.fetchone()
        if root is None:
            return None

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT holiday_date FROM gate_safety_strategy_earnings_dates WHERE gate_safety_strategy_id = %s ORDER BY holiday_date",
                (gate_safety_strategy_id,),
            )
            dates_rows = cur.fetchall()
        earnings_dates = [str(r["holiday_date"]) for r in dates_rows] if dates_rows else []

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT epsilon_band, threshold_hedge_shares, max_delta_limit, vol_window_min, stale_ts_threshold_ms, "
                "wide_spread_pct, extreme_spread_pct, data_lag_threshold_ms FROM gate_safety_state WHERE gate_safety_strategy_id = %s",
                (gate_safety_strategy_id,),
            )
            state_row = cur.fetchone()
        if state_row is None:
            return None

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT min_hedge_shares, cooldown_seconds, max_hedge_shares_per_order, min_price_move_pct "
                "FROM gate_safety_intent WHERE gate_safety_strategy_id = %s",
                (gate_safety_strategy_id,),
            )
            intent_row = cur.fetchone()
        if intent_row is None:
            return None

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT max_daily_hedge_count, max_position_shares, max_daily_loss_usd, max_net_delta_shares, "
                "max_spread_pct, paper_trade FROM gate_safety_guard WHERE gate_safety_strategy_id = %s",
                (gate_safety_strategy_id,),
            )
            guard_row = cur.fetchone()
        if guard_row is None:
            return None

        strategy = {
            "structure": {
                "min_dte": int(root["min_dte"]),
                "max_dte": int(root["max_dte"]),
                "atm_band_pct": float(root["atm_band_pct"]),
            },
            "earnings": {
                "blackout_days_before": int(root["blackout_days_before"]),
                "blackout_days_after": int(root["blackout_days_after"]),
                "dates": earnings_dates,
            },
            "trading_hours_only": bool(root["trading_hours_only"]),
        }
        state = {
            "delta": {
                "epsilon_band": int(state_row["epsilon_band"]),
                "threshold_hedge_shares": int(state_row["threshold_hedge_shares"]),
                "max_delta_limit": int(state_row["max_delta_limit"]),
            },
            "market": {
                "vol_window_min": int(state_row["vol_window_min"]),
                "stale_ts_threshold_ms": int(state_row["stale_ts_threshold_ms"]),
            },
            "liquidity": {
                "wide_spread_pct": float(state_row["wide_spread_pct"]),
                "extreme_spread_pct": float(state_row["extreme_spread_pct"]),
            },
            "system": {
                "data_lag_threshold_ms": int(state_row["data_lag_threshold_ms"]),
            },
        }
        intent = {
            "hedge": {
                "min_hedge_shares": int(intent_row["min_hedge_shares"]),
                "cooldown_seconds": int(intent_row["cooldown_seconds"]),
                "max_hedge_shares_per_order": int(intent_row["max_hedge_shares_per_order"]),
                "min_price_move_pct": float(intent_row["min_price_move_pct"]),
            }
        }
        guard = {
            "risk": {
                "max_daily_hedge_count": int(guard_row["max_daily_hedge_count"]),
                "max_position_shares": int(guard_row["max_position_shares"]),
                "max_daily_loss_usd": float(guard_row["max_daily_loss_usd"]),
                "max_net_delta_shares": int(guard_row["max_net_delta_shares"]),
                "max_spread_pct": float(guard_row["max_spread_pct"]),
                "paper_trade": bool(guard_row["paper_trade"]),
            }
        }
        return {
            "strategy": strategy,
            "state": state,
            "intent": intent,
            "guard": guard,
        }
    except Exception:
        return None


def get_active_gate_safety_strategy_id(conn: Any) -> Optional[int]:
    """Return settings.active_gate_safety_strategy_id for id=1, or None if missing/not set."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT active_gate_safety_strategy_id FROM settings WHERE id = 1"
            )
            row = cur.fetchone()
        if row is None or row.get("active_gate_safety_strategy_id") is None:
            return None
        return int(row["active_gate_safety_strategy_id"])
    except Exception:
        return None


def get_active_strategy_structure_id(conn: Any) -> Optional[int]:
    """Return settings.active_strategy_structure_id for id=1, or None if missing/not set."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT active_strategy_structure_id FROM settings WHERE id = 1"
            )
            row = cur.fetchone()
        if row is None or row.get("active_strategy_structure_id") is None:
            return None
        return int(row["active_strategy_structure_id"])
    except Exception:
        return None


def get_active_strategy_allocation_id(conn: Any) -> Optional[int]:
    """Return settings.active_strategy_allocation_id for id=1, or None if missing/not set."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT active_strategy_allocation_id FROM settings WHERE id = 1"
            )
            row = cur.fetchone()
        if row is None or row.get("active_strategy_allocation_id") is None:
            return None
        return int(row["active_strategy_allocation_id"])
    except Exception:
        return None


def get_gate_safety_name(conn: Any, gate_safety_strategy_id: int) -> Optional[str]:
    """Return name of the gate_safety_strategy row, or None if not found."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT name FROM gate_safety_strategy WHERE gate_safety_strategy_id = %s",
                (gate_safety_strategy_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return str(row["name"]) if row.get("name") is not None else None
    except Exception:
        return None


def get_gate_safety_full_by_id(conn: Any, gate_safety_strategy_id: int) -> Optional[Dict[str, Any]]:
    """Return full gate set for UI edit: metadata + gates (config shape) + earnings_dates array.
    Returns None if not found or any child row missing.
    """
    gates = get_gates_by_id(conn, gate_safety_strategy_id)
    if gates is None:
        return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT gate_safety_strategy_id, name, version,
                       dim_direction, dim_structure, dim_coverage, dim_risk, dim_volatility, dim_time,
                       is_active
                FROM gate_safety_strategy WHERE gate_safety_strategy_id = %s
                """,
                (gate_safety_strategy_id,),
            )
            root = cur.fetchone()
        if root is None:
            return None
        strategy_node = gates.get("strategy") or {}
        earnings_node = strategy_node.get("earnings") or {}
        earnings_dates = earnings_node.get("dates") or []
        return {
            "gate_safety_strategy_id": int(root["gate_safety_strategy_id"]),
            "name": str(root["name"]) if root.get("name") is not None else "",
            "version": int(root["version"]) if root.get("version") is not None else 1,
            "structure_type": None,
            "dim_direction": root.get("dim_direction"),
            "dim_structure": root.get("dim_structure"),
            "dim_coverage": root.get("dim_coverage"),
            "dim_risk": root.get("dim_risk"),
            "dim_volatility": root.get("dim_volatility"),
            "dim_time": root.get("dim_time"),
            "is_active": bool(root["is_active"]) if root.get("is_active") is not None else True,
            "gates": gates,
            "earnings_dates": [str(d) for d in earnings_dates],
        }
    except Exception:
        return None


def list_gate_safety_sets(conn: Any) -> List[Dict[str, Any]]:
    """Return list of gate_safety_strategy rows (metadata + six dims)."""
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT gate_safety_strategy_id, name, version,
                       dim_direction, dim_structure, dim_coverage, dim_risk, dim_volatility, dim_time,
                       is_active
                FROM gate_safety_strategy
                ORDER BY name
                """
            )
            rows = cur.fetchall()
        return [{**dict(r), "structure_type": None} for r in rows]
    except Exception:
        return []
