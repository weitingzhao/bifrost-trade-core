"""Contract-unit conventions for portfolio arithmetic.

Interactive Brokers reports an option position's ``avgCost`` **per contract**
(premium x multiplier) while quoting its price **per share**. Every place that
mixes the two produces a number wrong by the multiplier, and the result looks
like a plausible dollar figure rather than an error — which is how a 100x
overstatement of every payoff, capital-at-risk and stress figure reached two
pages and was rendered as data.

So the conversion lives here, once, and every reader of ``avg_cost`` goes
through it. A comment at each site would have been the same fix with no way to
find the sites.
"""

from __future__ import annotations

import math
from typing import Optional

#: US equity options settle 100 shares per contract.
OPTION_MULTIPLIER = 100.0


def is_option(sec_type: Optional[str]) -> bool:
    return (sec_type or "").strip().upper() == "OPT"


def option_cost_per_share(
    avg_cost: Optional[float],
    sec_type: Optional[str],
) -> Optional[float]:
    """IB's per-contract ``avgCost`` as a per-share premium.

    Stock cost basis is already per share and passes through untouched —
    dividing it would be the same error in the other direction. Returns None for
    anything unparseable rather than a zero, because a zero cost basis silently
    turns into "the whole position is profit".
    """
    if avg_cost is None:
        return None
    try:
        c = float(avg_cost)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(c):
        return None
    return c / OPTION_MULTIPLIER if is_option(sec_type) else c


def position_value(
    per_share: Optional[float],
    qty: Optional[float],
    sec_type: Optional[str],
) -> Optional[float]:
    """Scale a per-share figure to the whole position.

    The multiplier belongs with the unit conversion, not scattered at call
    sites: ``(price - cost) * qty * 100`` is only correct when both price and
    cost are per share, and the sites that got it wrong were the ones where that
    precondition was invisible.
    """
    if per_share is None or qty is None:
        return None
    try:
        v = float(per_share)
        q = float(qty)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(v) and math.isfinite(q)):
        return None
    return v * q * (OPTION_MULTIPLIER if is_option(sec_type) else 1.0)
