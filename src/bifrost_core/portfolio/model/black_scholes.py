"""Black-Scholes pricing, Delta and implied vol for the R-M8 model (V1.2).

Pure functions, no DB / IO. ``core`` uses them for per-leg Greeks and the
spot x IV stress grid.

The math is ``bifrost_core.pricing.black_scholes`` (TD-42); these wrappers pin the model's
conventions so its numbers stay bit-identical to the copy it used to carry: the guarded
(non-strict) formulas, the ``IV_POSITIONS_MODEL`` solver, and ``right == "C"`` meaning a call
(anything else, ``"c"`` and ``"CALL"`` included, is a put). The model's rate is
``RATE_POSITIONS_MODEL`` (0.04), passed in by ``core``.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from bifrost_core.pricing.black_scholes import (
    IV_POSITIONS_MODEL,
    erf_delta,
    implied_vol,
    price,
)


def _right(right: str) -> str:
    return "C" if right == "C" else "P"


def _bs_price(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    return price(S, K, T, r, sigma, _right(right))


def _bs_delta(S: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    return erf_delta(S, K, T, r, sigma, _right(right))


def _implied_vol(
    market_price: float, S: float, K: float, T: float, r: float, right: str,
    tol: float = 1e-6, max_iter: int = 100,
) -> Optional[float]:
    """Newton-Raphson IV solve. Returns None on failure."""
    return implied_vol(
        market_price, S, K, T, r, _right(right),
        convention=IV_POSITIONS_MODEL, tol=tol, max_iter=max_iter,
    )


def _years_to(expiry: Optional[date]) -> float:
    """Year fraction to an expiry, floored at 0. Expired legs have no time value."""
    if expiry is None:
        return 0.0
    return max((expiry - date.today()).days, 0) / 365.0
