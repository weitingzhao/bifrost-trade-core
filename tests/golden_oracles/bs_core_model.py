"""Legacy oracle: bifrost_core.portfolio.model.black_scholes before TD-42 (core 0.33.2, 84a63fb).

Verbatim copy of the pricing functions (lines 8-84). Do not edit (TD-42 golden test).
"""
# ruff: noqa


import math
from datetime import date
from typing import Optional


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


def _years_to(expiry: Optional[date]) -> float:
    """Year fraction to an expiry, floored at 0. Expired legs have no time value."""
    if expiry is None:
        return 0.0
    return max((expiry - date.today()).days, 0) / 365.0
