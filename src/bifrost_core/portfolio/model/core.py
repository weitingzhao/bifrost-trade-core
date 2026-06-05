"""R-M8 V1: compute_model_analysis — orchestrate per-underlying payoff + CAR + optional Greeks/stress.

Reads account_positions + contract_quote_live + account summary from DB, groups by underlying,
and returns the full model-analysis response dict.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import RealDictCursor

from bifrost_core.portfolio.model.payoff import (
    EnvelopeResult,
    RiskPosition,
    ScenarioBreakdown,
    compute_risk_profile,
    get_risk_grid_rows,
)

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "This analysis is hypothetical and based on model assumptions. "
    "It does not represent actual performance and is not investment advice. "
    "Options involve risk and may result in substantial losses."
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch_positions(conn: Any, account_id: str) -> List[Dict[str, Any]]:
    """Fetch positions with joined live quotes for a single account."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                ap.symbol,
                ap.sec_type,
                ap.position,
                ap.avg_cost,
                ap.expiry,
                ap.strike,
                ap.option_right,
                ap.contract_key,
                cq.mid  AS price_mid,
                cq.last AS price_last
            FROM account_positions ap
            LEFT JOIN contract_quote_live cq ON ap.contract_key = cq.contract_key
            WHERE ap.account_id = %s
            ORDER BY ap.symbol, ap.sec_type, ap.contract_key
            """,
            (account_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def _fetch_account_summary(conn: Any, account_id: str) -> Dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT net_liquidation, total_cash, buying_power FROM account WHERE account_id = %s",
            (account_id,),
        )
        row = cur.fetchone()
    if not row:
        return {}
    return {
        "net_liquidation": float(row["net_liquidation"]) if row.get("net_liquidation") is not None else None,
        "total_cash": float(row["total_cash"]) if row.get("total_cash") is not None else None,
        "buying_power": float(row["buying_power"]) if row.get("buying_power") is not None else None,
    }


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def _best_price(row: Dict[str, Any]) -> Optional[float]:
    for k in ("price_mid", "price_last"):
        v = row.get(k)
        if v is not None:
            try:
                f = float(v)
                if math.isfinite(f) and f > 0:
                    return f
            except (TypeError, ValueError):
                pass
    return None


def _parse_expiry(raw: Any) -> Optional[date]:
    if raw is None:
        return None
    s = str(raw).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:10] if "-" in s else s[:8], fmt).date()
        except ValueError:
            continue
    return None


def _dte(expiry_date: Optional[date]) -> Optional[int]:
    if expiry_date is None:
        return None
    delta = (expiry_date - date.today()).days
    return max(delta, 0)


def _group_positions(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Group raw DB rows into {underlying_symbol: {stock_row, opt_rows, spot}}."""
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sec = (r.get("sec_type") or "").strip().upper()
        sym = (r.get("symbol") or "").strip().upper()
        if not sym:
            continue
        qty = r.get("position")
        if qty is None:
            continue
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue

        if sym not in groups:
            groups[sym] = {"stock_qty": 0, "stock_avg_cost": None, "opt_positions": [], "spot": None, "farthest_expiry": None}

        g = groups[sym]
        if sec == "STK":
            g["stock_qty"] = int(qty)
            avg = r.get("avg_cost")
            if avg is not None:
                try:
                    g["stock_avg_cost"] = float(avg)
                except (TypeError, ValueError):
                    pass
            price = _best_price(r)
            if price is not None:
                g["spot"] = price
        elif sec == "OPT":
            strike = r.get("strike")
            right = (r.get("option_right") or "").strip().upper()
            avg_cost = r.get("avg_cost")
            if strike is None or right not in ("C", "P") or avg_cost is None:
                continue
            try:
                strike_f = float(strike)
                avg_cost_f = float(avg_cost)
            except (TypeError, ValueError):
                continue
            g["opt_positions"].append(RiskPosition(
                strike=strike_f,
                right=right,
                qty=int(qty),
                avg_cost=avg_cost_f,
            ))
            price = _best_price(r)
            if price is not None and g["spot"] is None:
                pass  # OPT price is option price, not underlying — spot comes from STK row or later
            exp = _parse_expiry(r.get("expiry"))
            if exp is not None:
                if g["farthest_expiry"] is None or exp > g["farthest_expiry"]:
                    g["farthest_expiry"] = exp
    return groups


# ---------------------------------------------------------------------------
# CAR (Capital at risk) — V1.1
# ---------------------------------------------------------------------------

def _compute_car_per_leg(p: RiskPosition, stock_avg_cost: Optional[float], stock_qty: int) -> Tuple[float, str]:
    """Heuristic single-leg CAR. Returns (car_value, car_type_label)."""
    if p.qty > 0:
        return abs(p.avg_cost) * abs(p.qty) * 100, "premium_paid"
    if p.right == "P" and p.qty < 0:
        return p.strike * abs(p.qty) * 100, "cash_secured"
    if p.right == "C" and p.qty < 0:
        covered = min(abs(p.qty) * 100, max(stock_qty, 0))
        if covered > 0 and stock_avg_cost is not None:
            return stock_avg_cost * covered, "covered_stock_cost"
        return float("inf"), "naked_unbounded"
    return 0.0, "unknown"


def _compute_car(
    opt_positions: List[RiskPosition],
    stock_qty: int,
    stock_avg_cost: Optional[float],
    envelope_max_loss: Optional[float],
) -> Dict[str, Any]:
    """Aggregate CAR with net-portfolio-max-loss check (PRD §4.3)."""
    leg_cars: List[Dict[str, Any]] = []
    total_leg = 0.0
    has_unbounded = False
    for p in opt_positions:
        val, label = _compute_car_per_leg(p, stock_avg_cost, stock_qty)
        if math.isinf(val):
            has_unbounded = True
            leg_cars.append({"strike": p.strike, "right": p.right, "qty": p.qty, "car": None, "type": label})
        else:
            total_leg += val
            leg_cars.append({"strike": p.strike, "right": p.right, "qty": p.qty, "car": round(val, 2), "type": label})

    net_max_loss = abs(envelope_max_loss) if envelope_max_loss is not None else None
    if net_max_loss is not None and not has_unbounded and net_max_loss < total_leg:
        effective = round(net_max_loss, 2)
        explain = "net_portfolio_max_loss"
    else:
        effective = round(total_leg, 2) if not has_unbounded else None
        explain = "sum_of_legs" if not has_unbounded else "unbounded"

    return {
        "effective": effective,
        "explain": explain,
        "has_unbounded": has_unbounded,
        "leg_details": leg_cars,
    }


# ---------------------------------------------------------------------------
# Annualized return — V1.1
# ---------------------------------------------------------------------------

def _annualized_return(profit: Optional[float], car: Optional[float], dte_days: Optional[int]) -> Optional[float]:
    if profit is None or car is None or car <= 0 or dte_days is None or dte_days <= 0:
        return None
    return round((profit / car) * (365.0 / dte_days), 6)


# ---------------------------------------------------------------------------
# BS implied vol + Delta — V1.2
# ---------------------------------------------------------------------------

def _bs_d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    if T <= 0:
        intr = max(S - K, 0.0) if right == "C" else max(K - S, 0.0)
        return intr
    d1 = _bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    if right == "C":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_delta(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    if T <= 0 or sigma <= 0:
        if right == "C":
            return 1.0 if S > K else (0.5 if S == K else 0.0)
        else:
            return -1.0 if S < K else (-0.5 if S == K else 0.0)
    d1 = _bs_d1(S, K, T, r, sigma)
    if right == "C":
        return _norm_cdf(d1)
    else:
        return _norm_cdf(d1) - 1.0


def _implied_vol(
    market_price: float, S: float, K: float, T: float, r: float, right: str,
    tol: float = 1e-6, max_iter: int = 100,
) -> Optional[float]:
    """Newton-Raphson IV solve. Returns None on failure."""
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return None
    intrinsic = max(S - K, 0.0) if right == "C" else max(K - S, 0.0)
    if market_price < intrinsic - tol:
        return None
    sigma = 0.3
    for _ in range(max_iter):
        price = _bs_price(S, K, T, r, sigma, right)
        d1 = _bs_d1(S, K, T, r, sigma)
        vega = S * math.sqrt(T) * math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        if vega < 1e-12:
            break
        diff = price - market_price
        if abs(diff) < tol:
            return sigma
        sigma -= diff / vega
        if sigma <= 0.001:
            sigma = 0.001
        if sigma > 5.0:
            return None
    return sigma if abs(_bs_price(S, K, T, r, sigma, right) - market_price) < 0.05 else None


def _compute_greeks_for_group(
    opt_positions: List[RiskPosition],
    stock_qty: int,
    spot: Optional[float],
    farthest_expiry: Optional[date],
    opt_mid_prices: Dict[Tuple[float, str], float],
    r: float = 0.04,
) -> Dict[str, Any]:
    """Compute portfolio delta for one underlying group. Returns delta info dict."""
    if spot is None or spot <= 0:
        return {"delta": None, "delta_dollars": None, "degraded": True, "reason": "no_spot"}

    T = None
    if farthest_expiry is not None:
        days = (farthest_expiry - date.today()).days
        T = max(days, 0) / 365.0

    total_delta = float(stock_qty)
    degraded_legs = 0
    per_leg: List[Dict[str, Any]] = []

    for p in opt_positions:
        exp = farthest_expiry
        t = T if T is not None and T > 0 else 0.0
        mid_key = (p.strike, p.right)
        mid = opt_mid_prices.get(mid_key)
        iv: Optional[float] = None
        leg_delta: Optional[float] = None
        if mid is not None and mid > 0 and spot > 0 and t > 0:
            iv = _implied_vol(mid, spot, p.strike, t, r, p.right)
        if iv is not None and iv > 0 and t > 0:
            raw_delta = _bs_delta(spot, p.strike, t, r, iv, p.right)
            leg_delta = raw_delta * p.qty * 100
            total_delta += leg_delta
        else:
            degraded_legs += 1

        per_leg.append({
            "strike": p.strike, "right": p.right, "qty": p.qty,
            "iv": round(iv, 4) if iv is not None else None,
            "delta": round(leg_delta, 4) if leg_delta is not None else None,
        })

    delta_dollars = round(total_delta * spot, 2) if spot else None
    return {
        "delta": round(total_delta, 4),
        "delta_dollars": delta_dollars,
        "degraded": degraded_legs > 0,
        "degraded_leg_count": degraded_legs,
        "per_leg": per_leg,
    }


# ---------------------------------------------------------------------------
# Stress matrix — V1.3
# ---------------------------------------------------------------------------

SPOT_SHOCKS = [-0.10, -0.05, 0.05, 0.10]
IV_SHOCKS = [-0.05, 0.05]  # absolute vol points


def _stress_matrix(
    opt_positions: List[RiskPosition],
    stock_qty: int,
    stock_avg_cost: Optional[float],
    spot: Optional[float],
    farthest_expiry: Optional[date],
    opt_mid_prices: Dict[Tuple[float, str], float],
    r: float = 0.04,
) -> Dict[str, Any]:
    """Compute P&L matrix for spot shocks x IV shocks."""
    if spot is None or spot <= 0:
        return {"available": False, "reason": "no_spot"}

    T = 0.0
    if farthest_expiry is not None:
        days = (farthest_expiry - date.today()).days
        T = max(days, 0) / 365.0

    # Compute base IVs for each leg
    leg_ivs: Dict[int, Optional[float]] = {}
    for i, p in enumerate(opt_positions):
        mid = opt_mid_prices.get((p.strike, p.right))
        if mid and mid > 0 and T > 0:
            leg_ivs[i] = _implied_vol(mid, spot, p.strike, T, r, p.right)
        else:
            leg_ivs[i] = None

    iv_available = any(v is not None for v in leg_ivs.values())

    scenarios: List[Dict[str, Any]] = []
    for spot_shock in SPOT_SHOCKS:
        new_spot = spot * (1.0 + spot_shock)
        # Intrinsic-only row (IV = base, no IV shock)
        intrinsic_pnl = 0.0
        for p in opt_positions:
            intr = max(new_spot - p.strike, 0.0) if p.right == "C" else max(p.strike - new_spot, 0.0)
            abs_qty = abs(p.qty)
            if p.qty > 0:
                intrinsic_pnl += (intr - p.avg_cost) * abs_qty * 100
            else:
                intrinsic_pnl += (p.avg_cost - intr) * abs_qty * 100
        stock_pnl = (new_spot - stock_avg_cost) * stock_qty if stock_qty and stock_avg_cost else 0.0

        base_row = {
            "spot_shock": spot_shock,
            "iv_shock": 0,
            "new_spot": round(new_spot, 2),
            "options_pnl": round(intrinsic_pnl, 2),
            "stock_pnl": round(stock_pnl, 2),
            "total_pnl": round(intrinsic_pnl + stock_pnl, 2),
            "method": "intrinsic",
        }

        # If we have IVs, compute BS-repriced rows for base + IV shocks
        if iv_available and T > 0:
            for iv_shock in [0.0] + IV_SHOCKS:
                opt_pnl = 0.0
                method = "bs_reprice"
                for i, p in enumerate(opt_positions):
                    base_iv = leg_ivs.get(i)
                    if base_iv is not None and base_iv > 0:
                        new_iv = max(base_iv + iv_shock, 0.01)
                        new_price = _bs_price(new_spot, p.strike, T, r, new_iv, p.right)
                        old_price = p.avg_cost
                        abs_qty = abs(p.qty)
                        if p.qty > 0:
                            opt_pnl += (new_price - old_price) * abs_qty * 100
                        else:
                            opt_pnl += (old_price - new_price) * abs_qty * 100
                    else:
                        intr = max(new_spot - p.strike, 0.0) if p.right == "C" else max(p.strike - new_spot, 0.0)
                        abs_qty = abs(p.qty)
                        if p.qty > 0:
                            opt_pnl += (intr - p.avg_cost) * abs_qty * 100
                        else:
                            opt_pnl += (p.avg_cost - intr) * abs_qty * 100
                        if iv_shock != 0:
                            method = "mixed_intrinsic"
                scenarios.append({
                    "spot_shock": spot_shock,
                    "iv_shock": iv_shock,
                    "new_spot": round(new_spot, 2),
                    "options_pnl": round(opt_pnl, 2),
                    "stock_pnl": round(stock_pnl, 2),
                    "total_pnl": round(opt_pnl + stock_pnl, 2),
                    "method": method,
                })
        else:
            scenarios.append(base_row)

    return {
        "available": True,
        "iv_stress_available": iv_available,
        "scenarios": scenarios,
    }


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------

def _scenario_to_dict(s: Optional[ScenarioBreakdown]) -> Optional[Dict[str, Any]]:
    if s is None:
        return None
    return {"underlying_price": s.underlying_price, "options_pnl": s.options_pnl, "stock_pnl": s.stock_pnl}


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def compute_model_analysis(conn: Any, account_id: str) -> Dict[str, Any]:
    """Top-level: fetch data, group by underlying, compute all V1 metrics, return response dict."""
    rows = _fetch_positions(conn, account_id)
    summary = _fetch_account_summary(conn, account_id)
    groups = _group_positions(rows)

    # Build per-leg mid prices lookup for greeks/stress (keyed by underlying)
    per_underlying_mids: Dict[str, Dict[Tuple[float, str], float]] = defaultdict(dict)
    for r in rows:
        sec = (r.get("sec_type") or "").strip().upper()
        sym = (r.get("symbol") or "").strip().upper()
        if sec == "OPT" and sym:
            strike = r.get("strike")
            right = (r.get("option_right") or "").strip().upper()
            mid = _best_price(r)
            if strike is not None and right in ("C", "P") and mid is not None:
                per_underlying_mids[sym][(float(strike), right)] = mid

    per_underlying: List[Dict[str, Any]] = []
    total_car = 0.0
    total_car_valid = True
    weighted_annual_num = 0.0
    weighted_annual_den = 0.0

    for sym in sorted(groups.keys()):
        g = groups[sym]
        opt = g["opt_positions"]
        stock_qty = g["stock_qty"]
        stock_avg = g["stock_avg_cost"]
        spot = g["spot"]
        farthest = g["farthest_expiry"]
        dte = _dte(farthest) if farthest else None

        # V1.0: payoff envelope
        profile = compute_risk_profile(opt, stock_qty, stock_avg)

        # V1.1: CAR + annualized
        car = _compute_car(opt, stock_qty, stock_avg, profile.max_loss)
        annual_max = _annualized_return(profile.max_gain, car["effective"], dte)
        annual_loss = _annualized_return(
            abs(profile.max_loss) if profile.max_loss is not None else None,
            car["effective"],
            dte,
        )

        if car["effective"] is not None:
            total_car += car["effective"]
            if dte and dte > 0 and profile.max_gain is not None:
                weighted_annual_num += (profile.max_gain / car["effective"]) * (365.0 / dte) * car["effective"]
                weighted_annual_den += car["effective"]
        else:
            total_car_valid = False

        # V1.2: Delta
        greeks = _compute_greeks_for_group(
            opt, stock_qty, spot, farthest,
            per_underlying_mids.get(sym, {}),
        )

        # V1.3: Stress
        stress = _stress_matrix(
            opt, stock_qty, stock_avg, spot, farthest,
            per_underlying_mids.get(sym, {}),
        )

        entry: Dict[str, Any] = {
            "symbol": sym,
            "spot": spot,
            "dte_days": dte,
            "dte_basis": "farthest_expiry",
            "farthest_expiry": farthest.isoformat() if farthest else None,
            "stock_qty": stock_qty,
            "stock_avg_cost": stock_avg,
            # Payoff envelope
            "max_gain": profile.max_gain,
            "max_loss": profile.max_loss,
            "risk_type": profile.risk_type,
            "breakeven_prices": profile.breakeven_prices,
            "net_premium": profile.net_premium,
            "naked_short_call_contracts": profile.naked_short_call_contracts,
            "hedged_max_loss": profile.hedged_max_loss,
            "max_gain_scenario": _scenario_to_dict(profile.max_gain_scenario),
            "max_gain_sample_scenario": _scenario_to_dict(profile.max_gain_sample_scenario),
            "max_loss_scenario": _scenario_to_dict(profile.max_loss_scenario),
            "hedged_max_loss_scenario": _scenario_to_dict(profile.hedged_max_loss_scenario),
            # CAR
            "capital_at_risk": car,
            # Annualized
            "annualized_return_on_car": annual_max,
            "annualized_loss_on_car": annual_loss,
            # Greeks
            "greeks": greeks,
            # Stress
            "stress": stress,
        }
        per_underlying.append(entry)

    # Account-level rollups
    account_rollups: Dict[str, Any] = {
        "total_car": round(total_car, 2) if total_car_valid else None,
        "car_has_unbounded": not total_car_valid,
        "weighted_annualized_return": (
            round(weighted_annual_num / weighted_annual_den, 6)
            if weighted_annual_den > 0 else None
        ),
        "total_delta": None,
        "total_delta_dollars": None,
    }

    # Aggregate delta across underlyings
    agg_delta = 0.0
    agg_delta_dollars = 0.0
    delta_valid = True
    for entry in per_underlying:
        g_info = entry.get("greeks") or {}
        d = g_info.get("delta")
        dd = g_info.get("delta_dollars")
        if d is not None:
            agg_delta += d
        else:
            delta_valid = False
        if dd is not None:
            agg_delta_dollars += dd
    if delta_valid or agg_delta != 0:
        account_rollups["total_delta"] = round(agg_delta, 4)
        account_rollups["total_delta_dollars"] = round(agg_delta_dollars, 2)

    # Aggregate stress scenarios across underlyings
    agg_stress = _aggregate_stress(per_underlying)

    return {
        "account_id": account_id,
        "account_summary": summary,
        "per_underlying": per_underlying,
        "account_rollups": account_rollups,
        "account_stress": agg_stress,
        "disclaimer": DISCLAIMER,
        "disclaimer_required": True,
        "method": "intrinsic_expiry_with_bs_greeks",
    }


def _aggregate_stress(per_underlying: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum stress scenarios across all underlyings."""
    combined: Dict[Tuple[float, float], Dict[str, float]] = {}
    any_available = False
    iv_available = False
    for entry in per_underlying:
        st = entry.get("stress") or {}
        if not st.get("available"):
            continue
        any_available = True
        if st.get("iv_stress_available"):
            iv_available = True
        for sc in st.get("scenarios", []):
            key = (sc["spot_shock"], sc["iv_shock"])
            if key not in combined:
                combined[key] = {"total_pnl": 0.0}
            combined[key]["total_pnl"] += sc["total_pnl"]

    if not any_available:
        return {"available": False}

    scenarios = [
        {"spot_shock": k[0], "iv_shock": k[1], "total_pnl": round(v["total_pnl"], 2)}
        for k, v in sorted(combined.items())
    ]
    return {"available": True, "iv_stress_available": iv_available, "scenarios": scenarios}
