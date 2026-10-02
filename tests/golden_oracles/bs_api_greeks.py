"""Legacy oracle: Black-Scholes as bifrost-trade-api research/routers/greeks.py has it.

Verbatim copy of lines 24-147 at api f94feb6 (origin/main, 2026-10-02): the rate constant
and every pricing function, without the FastAPI router. Do not edit -- it is the
reference the core functions must reproduce exactly (TD-42 golden test).
"""
# ruff: noqa

from __future__ import annotations

import math
from typing import Dict, Optional

DEFAULT_RISK_FREE_RATE = 0.045

# ---------------------------------------------------------------------------
# Black-Scholes math (pure Python, no scipy)
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_d1d2(S: float, K: float, T: float, r: float, sigma: float):
    """Return (d1, d2) for BS formula."""
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    """Black-Scholes call (right='C') or put (right='P') price."""
    d1, d2 = _bs_d1d2(S, K, T, r, sigma)
    if right.upper() == "C":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Vega = dPrice/dSigma (same for calls and puts)."""
    d1, _ = _bs_d1d2(S, K, T, r, sigma)
    return S * _norm_pdf(d1) * math.sqrt(T)


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    right: str,
    max_iter: int = 50,
) -> Optional[float]:
    """Newton-Raphson implied volatility.

    Returns None when IV cannot be found (deep OTM/ITM, zero price, etc.).
    """
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return None

    # Intrinsic value floor
    if right.upper() == "C":
        intrinsic = max(0.0, S - K * math.exp(-r * T))
    else:
        intrinsic = max(0.0, K * math.exp(-r * T) - S)

    if market_price < intrinsic - 1e-6:
        return None

    sigma = 0.3  # initial guess
    for _ in range(max_iter):
        try:
            price = _bs_price(S, K, T, r, sigma, right)
            vega = _bs_vega(S, K, T, r, sigma)
            if vega < 1e-10:
                break
            diff = price - market_price
            sigma -= diff / vega
            sigma = max(0.001, min(5.0, sigma))
            if abs(diff) < 1e-8:
                break
        except (ValueError, ZeroDivisionError):
            break

    # Validate: price should be close to market
    try:
        check = _bs_price(S, K, T, r, sigma, right)
        if abs(check - market_price) > max(0.05 * market_price, 0.05):
            return None
    except (ValueError, ZeroDivisionError):
        return None

    return sigma if 0.001 <= sigma <= 5.0 else None


def compute_greeks(
    S: float, K: float, T: float, r: float, sigma: float, right: str
) -> Dict[str, float]:
    """Compute Delta, Gamma, Theta (per calendar day), Vega (per 1% vol move).

    Returns dict with keys: delta, gamma, theta, vega.
    """
    d1, d2 = _bs_d1d2(S, K, T, r, sigma)
    nd1 = _norm_pdf(d1)
    sqrt_T = math.sqrt(T)
    discount = math.exp(-r * T)

    gamma = nd1 / (S * sigma * sqrt_T)

    if right.upper() == "C":
        delta = _norm_cdf(d1)
        theta_annual = (
            -(S * nd1 * sigma) / (2.0 * sqrt_T)
            - r * K * discount * _norm_cdf(d2)
        )
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_annual = (
            -(S * nd1 * sigma) / (2.0 * sqrt_T)
            + r * K * discount * _norm_cdf(-d2)
        )

    theta_per_day = theta_annual / 365.0
    vega_per_1pct = _bs_vega(S, K, T, r, sigma) * 0.01

    return {
        "delta": round(delta, 6),
        "gamma": round(gamma, 6),
        "theta": round(theta_per_day, 6),
        "vega": round(vega_per_1pct, 6),
    }
