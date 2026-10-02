"""Legacy oracle: the put ITM probability in bifrost-trade-api research/routers/screener.py.

Verbatim copy of RISK_FREE_RATE (line 19) and lines 56-73 at api f94feb6 (origin/main,
2026-10-02). Do not edit (TD-42 golden test).
"""
# ruff: noqa

from __future__ import annotations

import math

RISK_FREE_RATE = 0.045


def _bs_d1(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _prob_itm_put(spot: float, strike: float, dte: int, iv: float) -> float:
    """Probability of a put finishing in-the-money (BS N(-d2))."""
    T = dte / 365.0
    if T <= 0 or iv <= 0:
        return 1.0 if strike > spot else 0.0
    d1 = _bs_d1(spot, strike, T, RISK_FREE_RATE, iv)
    d2 = d1 - iv * math.sqrt(T)
    return _norm_cdf(-d2)
