"""Write gate_safety_* tables: create and update boundary sets. Used by POST/PUT gate-safety API."""

import logging
from typing import Any, Dict, List, Optional

import psycopg2

from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)


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


def _payload_to_root_and_children(payload: Dict[str, Any]) -> tuple[Dict[str, Any], List[str], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Extract root row fields, earnings_dates, state row, intent row, guard row from API payload."""
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

    root = {
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
    }

    state_row = {
        "epsilon_band": int(delta.get("epsilon_band", 10)),
        "threshold_hedge_shares": int(delta.get("threshold_hedge_shares", 25)),
        "max_delta_limit": int(delta.get("max_delta_limit", 500)),
        "vol_window_min": int(market.get("vol_window_min", 5)),
        "stale_ts_threshold_ms": int(market.get("stale_ts_threshold_ms", 5000)),
        "wide_spread_pct": float(liquidity.get("wide_spread_pct", 0.1)),
        "extreme_spread_pct": float(liquidity.get("extreme_spread_pct", 0.5)),
        "data_lag_threshold_ms": int(system.get("data_lag_threshold_ms", 1000)),
    }

    intent_row = {
        "min_hedge_shares": int(hedge.get("min_hedge_shares", 10)),
        "cooldown_seconds": int(hedge.get("cooldown_seconds", 60)),
        "max_hedge_shares_per_order": int(hedge.get("max_hedge_shares_per_order", 500)),
        "min_price_move_pct": float(hedge.get("min_price_move_pct", 0.2)),
    }

    guard_row = {
        "max_daily_hedge_count": int(risk.get("max_daily_hedge_count", 50)),
        "max_position_shares": int(risk.get("max_position_shares", 2000)),
        "max_daily_loss_usd": float(risk.get("max_daily_loss_usd", 5000.0)),
        "max_net_delta_shares": int(risk.get("max_net_delta_shares", 100)),
        "max_spread_pct": float(risk.get("max_spread_pct", 0.05)),
        "paper_trade": bool(risk.get("paper_trade", True)),
    }

    return root, earnings_dates, state_row, intent_row, guard_row


def create_gate_safety(status_config: Optional[dict], payload: Dict[str, Any]) -> Optional[int]:
    """Insert new gate_safety set and child rows. Returns gate_safety_strategy_id or None on error."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    try:
        root, earnings_dates, state_row, intent_row, guard_row = _payload_to_root_and_children(payload)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gate_safety_strategy (
                    name, version, dim_direction, dim_structure, dim_coverage,
                    dim_risk, dim_volatility, dim_time, is_active,
                    min_dte, max_dte, atm_band_pct, blackout_days_before, blackout_days_after, trading_hours_only
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING gate_safety_strategy_id
                """,
                (
                    root["name"],
                    root["version"],
                    root["dim_direction"],
                    root["dim_structure"],
                    root["dim_coverage"],
                    root["dim_risk"],
                    root["dim_volatility"],
                    root["dim_time"],
                    root["is_active"],
                    root["min_dte"],
                    root["max_dte"],
                    root["atm_band_pct"],
                    root["blackout_days_before"],
                    root["blackout_days_after"],
                    root["trading_hours_only"],
                ),
            )
            row = cur.fetchone()
            if not row:
                return None
            gid = int(row[0])
            for d in earnings_dates:
                if d and len(d) >= 10:
                    cur.execute(
                        "INSERT INTO gate_safety_strategy_earnings_dates (gate_safety_strategy_id, holiday_date) VALUES (%s, %s::date) ON CONFLICT DO NOTHING",
                        (gid, d),
                    )
            cur.execute(
                """
                INSERT INTO gate_safety_state (
                    gate_safety_strategy_id, epsilon_band, threshold_hedge_shares, max_delta_limit,
                    vol_window_min, stale_ts_threshold_ms, wide_spread_pct, extreme_spread_pct, data_lag_threshold_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    gid, state_row["epsilon_band"], state_row["threshold_hedge_shares"], state_row["max_delta_limit"],
                    state_row["vol_window_min"], state_row["stale_ts_threshold_ms"],
                    state_row["wide_spread_pct"], state_row["extreme_spread_pct"], state_row["data_lag_threshold_ms"],
                ),
            )
            cur.execute(
                """
                INSERT INTO gate_safety_intent (
                    gate_safety_strategy_id, min_hedge_shares, cooldown_seconds, max_hedge_shares_per_order, min_price_move_pct
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (gid, intent_row["min_hedge_shares"], intent_row["cooldown_seconds"], intent_row["max_hedge_shares_per_order"], intent_row["min_price_move_pct"]),
            )
            cur.execute(
                """
                INSERT INTO gate_safety_guard (
                    gate_safety_strategy_id, max_daily_hedge_count, max_position_shares, max_daily_loss_usd,
                    max_net_delta_shares, max_spread_pct, paper_trade
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (gid, guard_row["max_daily_hedge_count"], guard_row["max_position_shares"], guard_row["max_daily_loss_usd"],
                 guard_row["max_net_delta_shares"], guard_row["max_spread_pct"], guard_row["paper_trade"]),
            )
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
    """Update existing gate_safety set and child rows. Returns True on success."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        root, earnings_dates, state_row, intent_row, guard_row = _payload_to_root_and_children(payload)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gate_safety_strategy SET
                    name = %s, version = %s,
                    dim_direction = %s, dim_structure = %s, dim_coverage = %s,
                    dim_risk = %s, dim_volatility = %s, dim_time = %s,
                    is_active = %s,
                    min_dte = %s, max_dte = %s, atm_band_pct = %s,
                    blackout_days_before = %s, blackout_days_after = %s, trading_hours_only = %s,
                    updated_at = now()
                WHERE gate_safety_strategy_id = %s
                """,
                (
                    root["name"],
                    root["version"],
                    root["dim_direction"],
                    root["dim_structure"],
                    root["dim_coverage"],
                    root["dim_risk"],
                    root["dim_volatility"],
                    root["dim_time"],
                    root["is_active"],
                    root["min_dte"],
                    root["max_dte"],
                    root["atm_band_pct"],
                    root["blackout_days_before"],
                    root["blackout_days_after"],
                    root["trading_hours_only"],
                    gate_safety_strategy_id,
                ),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return False
            cur.execute("DELETE FROM gate_safety_strategy_earnings_dates WHERE gate_safety_strategy_id = %s", (gate_safety_strategy_id,))
            for d in earnings_dates:
                if d and len(d) >= 10:
                    cur.execute(
                        "INSERT INTO gate_safety_strategy_earnings_dates (gate_safety_strategy_id, holiday_date) VALUES (%s, %s::date) ON CONFLICT DO NOTHING",
                        (gate_safety_strategy_id, d),
                    )
            cur.execute(
                """
                INSERT INTO gate_safety_state (
                    gate_safety_strategy_id, epsilon_band, threshold_hedge_shares, max_delta_limit,
                    vol_window_min, stale_ts_threshold_ms, wide_spread_pct, extreme_spread_pct, data_lag_threshold_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (gate_safety_strategy_id) DO UPDATE SET
                    epsilon_band = EXCLUDED.epsilon_band, threshold_hedge_shares = EXCLUDED.threshold_hedge_shares,
                    max_delta_limit = EXCLUDED.max_delta_limit, vol_window_min = EXCLUDED.vol_window_min,
                    stale_ts_threshold_ms = EXCLUDED.stale_ts_threshold_ms, wide_spread_pct = EXCLUDED.wide_spread_pct,
                    extreme_spread_pct = EXCLUDED.extreme_spread_pct, data_lag_threshold_ms = EXCLUDED.data_lag_threshold_ms
                """,
                (
                    gate_safety_strategy_id, state_row["epsilon_band"], state_row["threshold_hedge_shares"], state_row["max_delta_limit"],
                    state_row["vol_window_min"], state_row["stale_ts_threshold_ms"],
                    state_row["wide_spread_pct"], state_row["extreme_spread_pct"], state_row["data_lag_threshold_ms"],
                ),
            )
            cur.execute(
                """
                INSERT INTO gate_safety_intent (
                    gate_safety_strategy_id, min_hedge_shares, cooldown_seconds, max_hedge_shares_per_order, min_price_move_pct
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (gate_safety_strategy_id) DO UPDATE SET
                    min_hedge_shares = EXCLUDED.min_hedge_shares, cooldown_seconds = EXCLUDED.cooldown_seconds,
                    max_hedge_shares_per_order = EXCLUDED.max_hedge_shares_per_order, min_price_move_pct = EXCLUDED.min_price_move_pct
                """,
                (gate_safety_strategy_id, intent_row["min_hedge_shares"], intent_row["cooldown_seconds"], intent_row["max_hedge_shares_per_order"], intent_row["min_price_move_pct"]),
            )
            cur.execute(
                """
                INSERT INTO gate_safety_guard (
                    gate_safety_strategy_id, max_daily_hedge_count, max_position_shares, max_daily_loss_usd,
                    max_net_delta_shares, max_spread_pct, paper_trade
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (gate_safety_strategy_id) DO UPDATE SET
                    max_daily_hedge_count = EXCLUDED.max_daily_hedge_count, max_position_shares = EXCLUDED.max_position_shares,
                    max_daily_loss_usd = EXCLUDED.max_daily_loss_usd, max_net_delta_shares = EXCLUDED.max_net_delta_shares,
                    max_spread_pct = EXCLUDED.max_spread_pct, paper_trade = EXCLUDED.paper_trade
                """,
                (gate_safety_strategy_id, guard_row["max_daily_hedge_count"], guard_row["max_position_shares"], guard_row["max_daily_loss_usd"],
                 guard_row["max_net_delta_shares"], guard_row["max_spread_pct"], guard_row["paper_trade"]),
            )
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
