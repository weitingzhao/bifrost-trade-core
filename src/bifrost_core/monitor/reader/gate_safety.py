"""Gate safety: load gates from DB by gate_safety_strategy_id. Shape compatible with config['gates'] for get_hedge_config."""

import json
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.schemas.gate_params import GateParams

_GATE_SAFETY_SELECT = """
    SELECT gate_safety_strategy_id, name, version,
           dim_direction, dim_structure, dim_coverage, dim_risk, dim_volatility, dim_time,
           is_active, params_json
    FROM gate_safety_strategy
    WHERE gate_safety_strategy_id = %s
"""


def build_gate_params_from_flat_row(row: Dict[str, Any], earnings_dates: List[str]) -> Dict[str, Any]:
    """Assemble nested config['gates'] dict from legacy flattened columns (Wave 9 migration)."""
    strategy = {
        "structure": {
            "min_dte": int(row["min_dte"]),
            "max_dte": int(row["max_dte"]),
            "atm_band_pct": float(row["atm_band_pct"]),
        },
        "earnings": {
            "blackout_days_before": int(row["blackout_days_before"]),
            "blackout_days_after": int(row["blackout_days_after"]),
            "dates": list(earnings_dates),
        },
        "trading_hours_only": bool(row["trading_hours_only"]),
    }
    state = {
        "delta": {
            "epsilon_band": int(row["epsilon_band"]),
            "threshold_hedge_shares": int(row["threshold_hedge_shares"]),
            "max_delta_limit": int(row["max_delta_limit"]),
        },
        "market": {
            "vol_window_min": int(row["vol_window_min"]),
            "stale_ts_threshold_ms": int(row["stale_ts_threshold_ms"]),
        },
        "liquidity": {
            "wide_spread_pct": float(row["wide_spread_pct"]),
            "extreme_spread_pct": float(row["extreme_spread_pct"]),
        },
        "system": {
            "data_lag_threshold_ms": int(row["data_lag_threshold_ms"]),
        },
    }
    intent = {
        "hedge": {
            "min_hedge_shares": int(row["min_hedge_shares"]),
            "cooldown_seconds": int(row["cooldown_seconds"]),
            "max_hedge_shares_per_order": int(row["max_hedge_shares_per_order"]),
            "min_price_move_pct": float(row["min_price_move_pct"]),
        }
    }
    guard = {
        "risk": {
            "max_daily_hedge_count": int(row["max_daily_hedge_count"]),
            "max_position_shares": int(row["max_position_shares"]),
            "max_daily_loss_usd": float(row["max_daily_loss_usd"]),
            "max_net_delta_shares": int(row["max_net_delta_shares"]),
            "max_spread_pct": float(row["max_spread_pct"]),
            "paper_trade": bool(row["paper_trade"]),
        }
    }
    return {
        "strategy": strategy,
        "state": state,
        "intent": intent,
        "guard": guard,
    }


def _parse_params_json(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return GateParams().model_dump()
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict):
        return GateParams.model_validate(raw).model_dump()
    return GateParams().model_dump()


def _row_to_gates(row: Dict[str, Any], earnings_dates: Optional[List[str]] = None) -> Dict[str, Any]:
    """Return config['gates'] dict from params_json or legacy flat columns."""
    if row.get("params_json") is not None:
        gates = _parse_params_json(row["params_json"])
        if earnings_dates is not None:
            strategy = gates.setdefault("strategy", {})
            earnings = strategy.setdefault("earnings", {})
            earnings["dates"] = earnings_dates
        return gates
    return build_gate_params_from_flat_row(row, earnings_dates or [])


def _load_earnings_dates(conn: Any, gate_safety_strategy_id: int) -> List[str]:
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT holiday_date FROM gate_safety_strategy_earnings_dates
                WHERE gate_safety_strategy_id = %s ORDER BY holiday_date
                """,
                (gate_safety_strategy_id,),
            )
            dates_rows = cur.fetchall()
        return [str(r["holiday_date"]) for r in dates_rows] if dates_rows else []
    except Exception:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT params_json FROM gate_safety_strategy WHERE gate_safety_strategy_id = %s",
                (gate_safety_strategy_id,),
            )
            row = cur.fetchone()
        if not row:
            return []
        gates = _parse_params_json(row.get("params_json"))
        dates = gates.get("strategy", {}).get("earnings", {}).get("dates") or []
        return [str(d) for d in dates if d]


def _extract_earnings_dates(row_dict: Dict[str, Any]) -> List[str]:
    """Earnings dates from params_json when collapsed; legacy rows use child table via conn."""
    if row_dict.get("params_json") is not None:
        gates = _parse_params_json(row_dict["params_json"])
        dates = gates.get("strategy", {}).get("earnings", {}).get("dates") or []
        return [str(d) for d in dates if d]
    return []


def get_gates_by_id(conn: Any, gate_safety_strategy_id: int) -> Optional[Dict[str, Any]]:
    """Load gates from gate_safety_strategy and return a dict in the shape of config['gates'].
    So the caller can set config['gates'] = get_gates_by_id(conn, id) and get_hedge_config(config) will work.
    Returns None if the boundary set is missing.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(_GATE_SAFETY_SELECT, (gate_safety_strategy_id,))
            row = cur.fetchone()
        if row is None:
            return None
        row_dict = dict(row)
        earnings_dates = _extract_earnings_dates(row_dict)
        if not earnings_dates and row_dict.get("params_json") is None:
            earnings_dates = _load_earnings_dates(conn, gate_safety_strategy_id)
        return _row_to_gates(row_dict, earnings_dates)
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
    Returns None if not found.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(_GATE_SAFETY_SELECT, (gate_safety_strategy_id,))
            row = cur.fetchone()
        if row is None:
            return None
        row_dict = dict(row)
        earnings_dates = _extract_earnings_dates(row_dict)
        if not earnings_dates and row_dict.get("params_json") is None:
            earnings_dates = _load_earnings_dates(conn, gate_safety_strategy_id)
        gates = _row_to_gates(row_dict, earnings_dates)
        return {
            "gate_safety_strategy_id": int(row["gate_safety_strategy_id"]),
            "name": str(row["name"]) if row.get("name") is not None else "",
            "version": int(row["version"]) if row.get("version") is not None else 1,
            "structure_type": None,
            "dim_direction": row.get("dim_direction"),
            "dim_structure": row.get("dim_structure"),
            "dim_coverage": row.get("dim_coverage"),
            "dim_risk": row.get("dim_risk"),
            "dim_volatility": row.get("dim_volatility"),
            "dim_time": row.get("dim_time"),
            "is_active": bool(row["is_active"]) if row.get("is_active") is not None else True,
            "gates": gates,
            "earnings_dates": earnings_dates,
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
