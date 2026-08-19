"""Write gate_safety_strategy + earnings_dates. Used by POST/PUT gate-safety API."""

import logging
from typing import Any, Dict, List, Optional

import psycopg2

from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)

_STRATEGY_COLUMNS = (
    "name",
    "version",
    "dim_direction",
    "dim_structure",
    "dim_coverage",
    "dim_risk",
    "dim_volatility",
    "dim_time",
    "is_active",
    "min_dte",
    "max_dte",
    "atm_band_pct",
    "blackout_days_before",
    "blackout_days_after",
    "trading_hours_only",
    "epsilon_band",
    "threshold_hedge_shares",
    "max_delta_limit",
    "vol_window_min",
    "stale_ts_threshold_ms",
    "wide_spread_pct",
    "extreme_spread_pct",
    "data_lag_threshold_ms",
    "min_hedge_shares",
    "cooldown_seconds",
    "max_hedge_shares_per_order",
    "min_price_move_pct",
    "max_daily_hedge_count",
    "max_position_shares",
    "max_daily_loss_usd",
    "max_net_delta_shares",
    "max_spread_pct",
    "paper_trade",
)


def _conn_from_config(status_config: Optional[dict]) -> Any:
    """Open a connection from status_config (postgres). Returns None if config invalid."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        params = _get_conn_params(status_config)
        return psycopg2.connect(**params)
    except Exception as e:
        logger.warning("gate_safety_write connect failed: %s", e)
        return None


def _payload_to_row(payload: Dict[str, Any]) -> tuple[Dict[str, Any], List[str]]:
    """Extract flattened gate_safety_strategy row + earnings_dates from API payload."""
    gates = payload.get("gates") or {}
    strategy = gates.get("strategy") or {}
    structure = strategy.get("structure") or {}
    earnings = strategy.get("earnings") or {}
    state = gates.get("state") or {}
    delta = state.get("delta") or {}
    market = state.get("market") or {}
    liquidity = state.get("liquidity") or {}
    system = state.get("system") or {}
    intent = gates.get("intent") or {}
    hedge = intent.get("hedge") or {}
    guard = gates.get("guard") or {}
    risk = guard.get("risk") or {}

    earnings_dates = payload.get("earnings_dates")
    if earnings_dates is None:
        earnings_dates = earnings.get("dates") or []
    earnings_dates = [str(d).strip()[:10] for d in earnings_dates if d]

    def _dim(k: str) -> Optional[str]:
        v = payload.get(k)
        if v is None or str(v).strip() == "":
            return None
        return str(v).strip()

    row = {
        "name": (payload.get("name") or "").strip() or "Unnamed",
        "version": int(payload["version"]) if payload.get("version") is not None else 1,
        "dim_direction": _dim("dim_direction"),
        "dim_structure": _dim("dim_structure"),
        "dim_coverage": _dim("dim_coverage"),
        "dim_risk": _dim("dim_risk"),
        "dim_volatility": _dim("dim_volatility"),
        "dim_time": _dim("dim_time"),
        "is_active": bool(payload["is_active"]) if payload.get("is_active") is not None else True,
        "min_dte": int(structure.get("min_dte", 21)),
        "max_dte": int(structure.get("max_dte", 35)),
        "atm_band_pct": float(structure.get("atm_band_pct", 0.03)),
        "blackout_days_before": int(earnings.get("blackout_days_before", 3)),
        "blackout_days_after": int(earnings.get("blackout_days_after", 1)),
        "trading_hours_only": bool(strategy.get("trading_hours_only", True)),
        "epsilon_band": int(delta.get("epsilon_band", 10)),
        "threshold_hedge_shares": int(delta.get("threshold_hedge_shares", 25)),
        "max_delta_limit": int(delta.get("max_delta_limit", 500)),
        "vol_window_min": int(market.get("vol_window_min", 5)),
        "stale_ts_threshold_ms": int(market.get("stale_ts_threshold_ms", 5000)),
        "wide_spread_pct": float(liquidity.get("wide_spread_pct", 0.1)),
        "extreme_spread_pct": float(liquidity.get("extreme_spread_pct", 0.5)),
        "data_lag_threshold_ms": int(system.get("data_lag_threshold_ms", 1000)),
        "min_hedge_shares": int(hedge.get("min_hedge_shares", 10)),
        "cooldown_seconds": int(hedge.get("cooldown_seconds", 60)),
        "max_hedge_shares_per_order": int(hedge.get("max_hedge_shares_per_order", 500)),
        "min_price_move_pct": float(hedge.get("min_price_move_pct", 0.2)),
        "max_daily_hedge_count": int(risk.get("max_daily_hedge_count", 50)),
        "max_position_shares": int(risk.get("max_position_shares", 2000)),
        "max_daily_loss_usd": float(risk.get("max_daily_loss_usd", 5000.0)),
        "max_net_delta_shares": int(risk.get("max_net_delta_shares", 100)),
        "max_spread_pct": float(risk.get("max_spread_pct", 0.05)),
        "paper_trade": bool(risk.get("paper_trade", True)),
    }
    return row, earnings_dates


def _replace_earnings_dates(cur: Any, gid: int, earnings_dates: List[str]) -> None:
    cur.execute(
        "DELETE FROM gate_safety_strategy_earnings_dates WHERE gate_safety_strategy_id = %s",
        (gid,),
    )
    for d in earnings_dates:
        if d and len(d) >= 10:
            cur.execute(
                "INSERT INTO gate_safety_strategy_earnings_dates (gate_safety_strategy_id, holiday_date) VALUES (%s, %s::date) ON CONFLICT DO NOTHING",
                (gid, d),
            )


def create_gate_safety(status_config: Optional[dict], payload: Dict[str, Any]) -> Optional[int]:
    """Insert a new gate_safety_strategy row + earnings_dates. Returns id or None on error."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    try:
        row, earnings_dates = _payload_to_row(payload)
        cols = ", ".join(_STRATEGY_COLUMNS)
        placeholders = ", ".join(["%s"] * len(_STRATEGY_COLUMNS))
        values = tuple(row[c] for c in _STRATEGY_COLUMNS)
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO gate_safety_strategy ({cols}) VALUES ({placeholders}) RETURNING gate_safety_strategy_id",
                values,
            )
            fetched = cur.fetchone()
            if not fetched:
                return None
            gid = int(fetched[0])
            _replace_earnings_dates(cur, gid, earnings_dates)
        conn.commit()
        return gid
    except Exception as e:
        logger.warning("create_gate_safety failed: %s", e)
        conn.rollback()
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def update_gate_safety(status_config: Optional[dict], gate_safety_strategy_id: int, payload: Dict[str, Any]) -> bool:
    """Update an existing gate_safety_strategy row + earnings_dates. Returns True on success."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        row, earnings_dates = _payload_to_row(payload)
        assignments = ", ".join(f"{c} = %s" for c in _STRATEGY_COLUMNS)
        values = tuple(row[c] for c in _STRATEGY_COLUMNS) + (gate_safety_strategy_id,)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE gate_safety_strategy SET {assignments}, updated_at = now() WHERE gate_safety_strategy_id = %s",
                values,
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
            _replace_earnings_dates(cur, gate_safety_strategy_id, earnings_dates)
        conn.commit()
        return True
    except Exception as e:
        logger.warning("update_gate_safety failed: %s", e)
        conn.rollback()
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
