"""Black-Scholes for every Python caller in Trade (TD-42).

Two families live here, and they are deliberately not merged:

* ``delta`` / ``gamma`` -- py_vollib, as the daemon's portfolio delta/gamma has always used
  them (``portfolio.positions.portfolio``). py_vollib's normal CDF is not ``math.erf``, so
  these differ from the erf family in the last bits; they stay as they are.
* everything else -- closed form on ``math.erf``: ``norm_cdf``, ``norm_pdf``, ``d1``, ``d2``,
  ``price``, ``erf_delta``, ``erf_gamma``, ``vega``, ``theta``, ``greeks``, ``prob_itm`` and
  ``implied_vol``. The arithmetic is the one the Positions model (core) and the research
  greeks / screener (api) carried in their own copies, operation for operation, so each of
  them gets bit-identical floats from here (tests/test_golden_black_scholes.py).

Conventions a caller must choose, because the copies differed and numbers must not move:

* ``strict`` (all erf functions). ``False`` -- the Positions model's guards: ``d1`` is 0.0
  when T, sigma, S or K is <= 0; ``price`` is intrinsic at T <= 0; ``erf_delta`` is the
  expiry step (1 / 0.5 / 0); gamma, vega and theta are 0.0 on degenerate input; ``prob_itm``
  is 1 / 0 by moneyness. ``True`` -- the research api's raw formula: degenerate input raises
  ``ZeroDivisionError`` (T = 0, sigma = 0) or ``ValueError`` (T < 0, S <= 0).
* ``implied_vol(..., convention=)``: ``IV_POSITIONS_MODEL`` or ``IV_RESEARCH``, the two
  Newton solvers with their own intrinsic floor, stopping rule and acceptance test.
* the risk-free rate. Each surface keeps its own; the ``RATE_*`` constants name them and say
  who uses which. Unifying them is an Owner decision that moves numbers -- not done here.

``right`` is a call when ``right.upper()`` is ``"C"`` or ``"CALL"``, anything else is a put.
The old copies were narrower (the model: ``right == "C"``; the api: ``right.upper() == "C"``),
so a caller moving here maps its own rule to ``"C"`` / ``"P"`` first.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, Optional

try:
    from py_vollib.black_scholes.greeks.analytical import delta as _delta, gamma as _gamma
    PY_VOLLIB_AVAILABLE = True
except ImportError:
    PY_VOLLIB_AVAILABLE = False

logger = logging.getLogger(__name__)

# --- risk-free rates in use (do not unify here; see module docstring) ------------------------
RATE_RESEARCH_GEX = 0.0
"""bifrost-research engines/gex/exposure.py ``approx_bs_gamma`` (r = q = 0). Its own copy."""
RATE_POSITIONS_MODEL = 0.04
"""Positions model (``portfolio.model.core``): per-leg IV, delta and the stress grid."""
RATE_SYMBOL_CHAIN_UI = 0.043
"""Frontend ``SymbolChainFace.tsx`` (TypeScript). Listed so the spread is visible in one place."""
RATE_RESEARCH = 0.045
"""Trade api research: ``greeks.py`` DEFAULT_RISK_FREE_RATE and ``screener.py`` RISK_FREE_RATE."""
RATE_DAEMON_DEFAULT = 0.05
"""Daemon portfolio delta/gamma: worker's fallback for ``greeks.risk_free_rate``; every
``config.*.yaml`` sets 0.05 as well."""

IV_POSITIONS_MODEL = "positions_model"
IV_RESEARCH = "research"


# --- py_vollib (daemon) --------------------------------------------------------------------


def delta(
    underlying_price: float,
    strike: float,
    time_to_expiration: float,
    risk_free_rate: float,
    volatility: float,
    option_type: str,
) -> float:
    """Option delta (per unit), py_vollib. option_type: 'call' or 'put'."""
    if time_to_expiration <= 0:
        return 0.0
    if not PY_VOLLIB_AVAILABLE:
        logger.error("py_vollib not available")
        return 0.0
    try:
        flag = "c" if option_type.upper() in ("C", "CALL") else "p"
        return float(_delta(flag, underlying_price, strike, time_to_expiration, risk_free_rate, volatility))
    except Exception as e:
        logger.error("BS delta error: %s", e)
        return 0.0


def gamma(
    underlying_price: float,
    strike: float,
    time_to_expiration: float,
    risk_free_rate: float,
    volatility: float,
    option_type: str,
) -> float:
    """Option gamma (per unit), py_vollib. option_type: 'call' or 'put'."""
    if time_to_expiration <= 0:
        return 0.0
    if not PY_VOLLIB_AVAILABLE:
        logger.error("py_vollib not available")
        return 0.0
    try:
        flag = "c" if option_type.upper() in ("C", "CALL") else "p"
        return float(_gamma(flag, underlying_price, strike, time_to_expiration, risk_free_rate, volatility))
    except Exception as e:
        logger.error("BS gamma error: %s", e)
        return 0.0


def calculate_greeks(
    underlying_price: float,
    strike: float,
    time_to_expiration: float,
    risk_free_rate: float,
    volatility: float,
    option_type: str,
) -> dict:
    """Delta and gamma as ``delta`` / ``gamma`` (py_vollib); theta per calendar day and vega per
    vol point from the erf family. All four are 0.0 at or past expiry.

    No surface calls it (the worker re-exports it from ``daemon.pricing``); before core 0.34.0
    theta and vega were always 0.0.
    """
    d = delta(underlying_price, strike, time_to_expiration, risk_free_rate, volatility, option_type)
    g = gamma(underlying_price, strike, time_to_expiration, risk_free_rate, volatility, option_type)
    if time_to_expiration <= 0:
        return {"delta": d, "gamma": g, "theta": 0.0, "vega": 0.0}
    args = (underlying_price, strike, time_to_expiration, risk_free_rate, volatility)
    return {
        "delta": d,
        "gamma": g,
        "theta": theta(*args, option_type),
        "vega": vega(*args) * 0.01,
    }


# --- erf family ----------------------------------------------------------------------------


def is_call(right: Optional[str]) -> bool:
    return (right or "").upper() in ("C", "CALL")


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _degenerate(S: float, K: float, T: float, sigma: float) -> bool:
    return T <= 0 or sigma <= 0 or S <= 0 or K <= 0


def d1(S: float, K: float, T: float, r: float, sigma: float, *, strict: bool = False) -> float:
    """d1. Not strict: 0.0 when T, sigma, S or K is <= 0."""
    if not strict and _degenerate(S, K, T, sigma):
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def d2(S: float, K: float, T: float, r: float, sigma: float, *, strict: bool = False) -> float:
    return d1(S, K, T, r, sigma, strict=strict) - sigma * math.sqrt(T)


def price(
    S: float, K: float, T: float, r: float, sigma: float, right: str, *, strict: bool = False
) -> float:
    """Call or put price. Not strict: intrinsic (undiscounted) at T <= 0."""
    call = is_call(right)
    if not strict and T <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    d_1 = d1(S, K, T, r, sigma, strict=strict)
    d_2 = d_1 - sigma * math.sqrt(T)
    if call:
        return S * norm_cdf(d_1) - K * math.exp(-r * T) * norm_cdf(d_2)
    return K * math.exp(-r * T) * norm_cdf(-d_2) - S * norm_cdf(-d_1)


def erf_delta(
    S: float, K: float, T: float, r: float, sigma: float, right: str, *, strict: bool = False
) -> float:
    """Delta per unit. Not strict: at T <= 0 or sigma <= 0 the expiry step (0.5 at the money)."""
    call = is_call(right)
    if not strict and (T <= 0 or sigma <= 0):
        if call:
            return 1.0 if S > K else (0.5 if S == K else 0.0)
        return -1.0 if S < K else (-0.5 if S == K else 0.0)
    d_1 = d1(S, K, T, r, sigma, strict=strict)
    return norm_cdf(d_1) if call else norm_cdf(d_1) - 1.0


def erf_gamma(S: float, K: float, T: float, r: float, sigma: float, *, strict: bool = False) -> float:
    """Gamma per unit (same for calls and puts). Not strict: 0.0 on degenerate input."""
    if not strict and _degenerate(S, K, T, sigma):
        return 0.0
    return norm_pdf(d1(S, K, T, r, sigma, strict=strict)) / (S * sigma * math.sqrt(T))


def vega(S: float, K: float, T: float, r: float, sigma: float, *, strict: bool = False) -> float:
    """dPrice/dSigma per 1.00 of vol (multiply by 0.01 for a vol point). Not strict: 0.0 on
    degenerate input."""
    if not strict and _degenerate(S, K, T, sigma):
        return 0.0
    return S * norm_pdf(d1(S, K, T, r, sigma, strict=strict)) * math.sqrt(T)


def theta(
    S: float, K: float, T: float, r: float, sigma: float, right: str, *, strict: bool = False
) -> float:
    """Theta per calendar day (annual / 365). Not strict: 0.0 on degenerate input."""
    if not strict and _degenerate(S, K, T, sigma):
        return 0.0
    d_1 = d1(S, K, T, r, sigma, strict=strict)
    d_2 = d_1 - sigma * math.sqrt(T)
    nd1 = norm_pdf(d_1)
    sqrt_T = math.sqrt(T)
    discount = math.exp(-r * T)
    if is_call(right):
        theta_annual = -(S * nd1 * sigma) / (2.0 * sqrt_T) - r * K * discount * norm_cdf(d_2)
    else:
        theta_annual = -(S * nd1 * sigma) / (2.0 * sqrt_T) + r * K * discount * norm_cdf(-d_2)
    return theta_annual / 365.0


def greeks(
    S: float, K: float, T: float, r: float, sigma: float, right: str, *, strict: bool = False
) -> Dict[str, float]:
    """delta, gamma (per unit), theta (per calendar day), vega (per vol point), unrounded.

    With ``strict=True`` and rounded to 6 places this is the api research ``compute_greeks``.
    """
    return {
        "delta": erf_delta(S, K, T, r, sigma, right, strict=strict),
        "gamma": erf_gamma(S, K, T, r, sigma, strict=strict),
        "theta": theta(S, K, T, r, sigma, right, strict=strict),
        "vega": vega(S, K, T, r, sigma, strict=strict) * 0.01,
    }


def prob_itm(
    S: float, K: float, T: float, r: float, sigma: float, right: str, *, strict: bool = False
) -> float:
    """Risk-neutral probability of finishing in the money: N(d2) for a call, N(-d2) for a put.
    Not strict: at T <= 0 or sigma <= 0, 1.0 if in the money else 0.0."""
    call = is_call(right)
    if not strict and (T <= 0 or sigma <= 0):
        if call:
            return 1.0 if S > K else 0.0
        return 1.0 if K > S else 0.0
    d_2 = d1(S, K, T, r, sigma, strict=strict) - sigma * math.sqrt(T)
    return norm_cdf(d_2) if call else norm_cdf(-d_2)


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    right: str,
    *,
    convention: str,
    max_iter: Optional[int] = None,
    tol: float = 1e-6,
) -> Optional[float]:
    """Newton-Raphson implied vol, or None when there is none. ``convention`` picks the solver:

    ``IV_POSITIONS_MODEL`` (core Positions model): undiscounted intrinsic floor (minus ``tol``),
    stop when |price - market| < ``tol`` before stepping, sigma floored at 0.001 and given up
    above 5.0, ``max_iter`` 100, accept when the final price is within 0.05.

    ``IV_RESEARCH`` (api research greeks): discounted intrinsic floor (minus 1e-6), step then
    clamp sigma to [0.001, 5.0], stop once |diff| < 1e-8, ``max_iter`` 50, accept when within
    max(5% of the price, 0.05); ``tol`` is not used.
    """
    if convention == IV_POSITIONS_MODEL:
        return _iv_positions_model(
            market_price, S, K, T, r, right, tol, 100 if max_iter is None else max_iter
        )
    if convention == IV_RESEARCH:
        return _iv_research(market_price, S, K, T, r, right, 50 if max_iter is None else max_iter)
    raise ValueError(f"unknown implied_vol convention {convention!r}")


def _iv_positions_model(
    market_price: float, S: float, K: float, T: float, r: float, right: str, tol: float, max_iter: int
) -> Optional[float]:
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return None
    intrinsic = max(S - K, 0.0) if is_call(right) else max(K - S, 0.0)
    if market_price < intrinsic - tol:
        return None
    sigma = 0.3
    for _ in range(max_iter):
        p = price(S, K, T, r, sigma, right)
        d_1 = d1(S, K, T, r, sigma)
        # The model's own vega arithmetic (S * sqrt(T) * phi(d1)), not vega(): a different
        # rounding order would move its IVs in the last bits.
        v = S * math.sqrt(T) * math.exp(-0.5 * d_1 * d_1) / math.sqrt(2 * math.pi)
        if v < 1e-12:
            break
        diff = p - market_price
        if abs(diff) < tol:
            return sigma
        sigma -= diff / v
        if sigma <= 0.001:
            sigma = 0.001
        if sigma > 5.0:
            return None
    return sigma if abs(price(S, K, T, r, sigma, right) - market_price) < 0.05 else None


def _iv_research(
    market_price: float, S: float, K: float, T: float, r: float, right: str, max_iter: int
) -> Optional[float]:
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return None
    if is_call(right):
        intrinsic = max(0.0, S - K * math.exp(-r * T))
    else:
        intrinsic = max(0.0, K * math.exp(-r * T) - S)
    if market_price < intrinsic - 1e-6:
        return None
    sigma = 0.3
    for _ in range(max_iter):
        try:
            p = price(S, K, T, r, sigma, right, strict=True)
            v = vega(S, K, T, r, sigma, strict=True)
            if v < 1e-10:
                break
            diff = p - market_price
            sigma -= diff / v
            sigma = max(0.001, min(5.0, sigma))
            if abs(diff) < 1e-8:
                break
        except (ValueError, ZeroDivisionError):
            break
    try:
        check = price(S, K, T, r, sigma, right, strict=True)
        if abs(check - market_price) > max(0.05 * market_price, 0.05):
            return None
    except (ValueError, ZeroDivisionError):
        return None
    return sigma if 0.001 <= sigma <= 5.0 else None
