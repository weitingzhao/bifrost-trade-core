"""Intrinsic (expiration) payoff engine — Python port of frontend/src/utils/riskProfile.ts.

All functions are pure (no DB / IO); inputs are typed dicts or dataclass-like namedtuples.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class RiskPosition:
    strike: float
    right: str  # "C" or "P"
    qty: int  # +long / -short, in contracts
    avg_cost: float  # per-share premium (not per-contract)


@dataclass
class ScenarioBreakdown:
    underlying_price: float
    options_pnl: float
    stock_pnl: float


@dataclass
class GridRow:
    price: float
    options_pnl: float
    stock_pnl: float
    total: float


@dataclass
class EnvelopeResult:
    max_gain: Optional[float]
    max_loss: Optional[float]
    risk_type: str  # "defined" | "unlimited"
    breakeven_prices: List[float]
    max_gain_scenario: Optional[ScenarioBreakdown]
    max_gain_sample_scenario: Optional[ScenarioBreakdown]
    max_loss_scenario: Optional[ScenarioBreakdown]
    net_premium: float
    naked_short_call_contracts: int
    hedged_max_loss: Optional[float]
    hedged_max_loss_scenario: Optional[ScenarioBreakdown]
    stock_shares_modeled: int
    stock_avg_cost_known: bool


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def payoff_options_at_price(positions: List[RiskPosition], price: float) -> float:
    total = 0.0
    for p in positions:
        intrinsic = max(price - p.strike, 0.0) if p.right == "C" else max(p.strike - price, 0.0)
        abs_qty = abs(p.qty)
        if p.qty > 0:
            total += (intrinsic - p.avg_cost) * abs_qty * 100
        else:
            total += (p.avg_cost - intrinsic) * abs_qty * 100
    return total


def payoff_stock_at_price(covered_shares: int, avg_cost: Optional[float], price: float) -> float:
    if covered_shares <= 0 or avg_cost is None:
        return 0.0
    return (price - avg_cost) * covered_shares


def get_risk_grid_rows(
    positions: List[RiskPosition],
    covered_shares: int,
    underlying_avg_cost: Optional[float],
) -> List[GridRow]:
    if not positions and covered_shares <= 0:
        return []
    strikes = sorted({p.strike for p in positions})
    price_points = [0.0] + list(strikes)
    if strikes:
        price_points.append(strikes[-1] * 2)
    rows: List[GridRow] = []
    for price in price_points:
        opt = payoff_options_at_price(positions, price)
        stk = payoff_stock_at_price(covered_shares, underlying_avg_cost, price)
        rows.append(GridRow(price=price, options_pnl=opt, stock_pnl=stk, total=opt + stk))
    return rows


def _round2(v: float) -> float:
    return round(v * 100) / 100


def _round_scenario(s: ScenarioBreakdown) -> ScenarioBreakdown:
    return ScenarioBreakdown(
        underlying_price=_round2(s.underlying_price),
        options_pnl=_round2(s.options_pnl),
        stock_pnl=_round2(s.stock_pnl),
    )


def _strip_naked_short_calls(
    positions: List[RiskPosition], contracts: int
) -> List[RiskPosition]:
    if contracts <= 0:
        return list(positions)
    shorts = sorted(
        [(i, p) for i, p in enumerate(positions) if p.right == "C" and p.qty < 0],
        key=lambda x: -x[1].strike,
    )
    idx_to_new_qty: dict[int, int] = {}
    rem = contracts
    for i, p in shorts:
        if rem <= 0:
            break
        abs_q = abs(p.qty)
        dec = min(rem, abs_q)
        idx_to_new_qty[i] = p.qty + dec
        rem -= dec
    out: List[RiskPosition] = []
    for i, p in enumerate(positions):
        nq = idx_to_new_qty.get(i)
        if nq is not None:
            if nq != 0:
                out.append(RiskPosition(strike=p.strike, right=p.right, qty=nq, avg_cost=p.avg_cost))
        else:
            out.append(p)
    return out


def _compute_envelope(
    positions: List[RiskPosition],
    covered_shares: int,
    underlying_avg_cost: Optional[float],
) -> dict:
    if not positions and covered_shares <= 0:
        return dict(
            max_gain=0.0, max_loss=0.0, risk_type="defined",
            breakeven_prices=[], max_gain_scenario=None,
            max_gain_sample_scenario=None, max_loss_scenario=None,
        )

    net_short_call = sum(abs(p.qty) * 100 for p in positions if p.right == "C" and p.qty < 0)
    net_long_call = sum(p.qty * 100 for p in positions if p.right == "C" and p.qty > 0)
    uncovered_upside = net_short_call - net_long_call - covered_shares
    has_unlimited_downside = uncovered_upside > 0
    has_unlimited_upside = (net_long_call > net_short_call + covered_shares) or (
        covered_shares > 0 and net_short_call == 0
    )

    rows = get_risk_grid_rows(positions, covered_shares, underlying_avg_cost)
    payoffs = [(r.price, r.total) for r in rows]

    breakevens: List[float] = []
    for i in range(len(payoffs) - 1):
        a_price, a_pay = payoffs[i]
        b_price, b_pay = payoffs[i + 1]
        if (a_pay >= 0 and b_pay < 0) or (a_pay < 0 and b_pay >= 0):
            if b_price != a_price:
                t = a_pay / (a_pay - b_pay)
                breakevens.append(_round2(a_price + t * (b_price - a_price)))
        elif a_pay == 0.0 and a_price > 0:
            breakevens.append(a_price)
    if payoffs and payoffs[-1][1] == 0.0 and payoffs[-1][0] > 0:
        last_p = payoffs[-1][0]
        if last_p not in breakevens:
            breakevens.append(last_p)

    min_idx = max_idx = 0
    for i in range(1, len(rows)):
        if rows[i].total < rows[min_idx].total:
            min_idx = i
        if rows[i].total > rows[max_idx].total:
            max_idx = i

    min_row, max_row = rows[min_idx], rows[max_idx]

    max_loss_scenario = None if has_unlimited_downside else _round_scenario(
        ScenarioBreakdown(min_row.price, min_row.options_pnl, min_row.stock_pnl)
    )
    max_gain_scenario = None if has_unlimited_upside else _round_scenario(
        ScenarioBreakdown(max_row.price, max_row.options_pnl, max_row.stock_pnl)
    )
    max_gain_sample_scenario = _round_scenario(
        ScenarioBreakdown(max_row.price, max_row.options_pnl, max_row.stock_pnl)
    ) if rows else None

    min_payoff = min(r.total for r in rows) if rows else 0.0
    max_payoff = max(r.total for r in rows) if rows else 0.0

    return dict(
        max_gain=None if has_unlimited_upside else max_payoff,
        max_loss=None if has_unlimited_downside else min_payoff,
        risk_type="unlimited" if has_unlimited_downside else "defined",
        breakeven_prices=breakevens,
        max_gain_scenario=max_gain_scenario,
        max_gain_sample_scenario=max_gain_sample_scenario,
        max_loss_scenario=max_loss_scenario,
    )


def compute_risk_profile(
    positions: List[RiskPosition],
    covered_shares: int,
    underlying_avg_cost: Optional[float],
) -> EnvelopeResult:
    """Full risk profile for one underlying group (port of computeRiskProfile in TS)."""
    stock_shares_modeled = covered_shares
    stock_avg_cost_known = covered_shares <= 0 or underlying_avg_cost is not None

    if not positions:
        return EnvelopeResult(
            max_gain=0.0, max_loss=0.0, risk_type="defined",
            breakeven_prices=[], max_gain_scenario=None,
            max_gain_sample_scenario=None, max_loss_scenario=None,
            net_premium=0.0, naked_short_call_contracts=0,
            hedged_max_loss=None, hedged_max_loss_scenario=None,
            stock_shares_modeled=stock_shares_modeled,
            stock_avg_cost_known=stock_avg_cost_known,
        )

    net_premium = 0.0
    for p in positions:
        abs_qty = abs(p.qty)
        if p.qty < 0:
            net_premium += p.avg_cost * abs_qty * 100
        else:
            net_premium -= p.avg_cost * abs_qty * 100

    net_short_call = sum(abs(p.qty) * 100 for p in positions if p.right == "C" and p.qty < 0)
    net_long_call = sum(p.qty * 100 for p in positions if p.right == "C" and p.qty > 0)
    residual = max(0, net_short_call - net_long_call - covered_shares)
    naked_short_call_contracts = math.ceil(residual / 100) if residual > 0 else 0

    env = _compute_envelope(positions, covered_shares, underlying_avg_cost)

    hedged_max_loss: Optional[float] = None
    hedged_max_loss_scenario: Optional[ScenarioBreakdown] = None
    if naked_short_call_contracts > 0:
        hedged = _strip_naked_short_calls(positions, naked_short_call_contracts)
        hedged_env = _compute_envelope(hedged, covered_shares, underlying_avg_cost)
        if hedged_env["max_loss"] is not None:
            hedged_max_loss = _round2(hedged_env["max_loss"])
        hedged_max_loss_scenario = hedged_env["max_loss_scenario"]

    return EnvelopeResult(
        max_gain=_round2(env["max_gain"]) if env["max_gain"] is not None else None,
        max_loss=_round2(env["max_loss"]) if env["max_loss"] is not None else None,
        risk_type=env["risk_type"],
        breakeven_prices=env["breakeven_prices"],
        max_gain_scenario=env["max_gain_scenario"],
        max_gain_sample_scenario=env["max_gain_sample_scenario"],
        max_loss_scenario=env["max_loss_scenario"],
        net_premium=_round2(net_premium),
        naked_short_call_contracts=naked_short_call_contracts,
        hedged_max_loss=hedged_max_loss,
        hedged_max_loss_scenario=hedged_max_loss_scenario,
        stock_shares_modeled=stock_shares_modeled,
        stock_avg_cost_known=stock_avg_cost_known,
    )
