"""When a `contract_quote_live` row still counts as a price.

The only writer of `brokerage.contract_quote_live` is the trading daemon, and
D10 keeps it from writing. The table therefore holds whatever it last wrote --
in 2026-10 that was a dozen stock rows from 2026-03-16 -- and every reader that
joined it without asking how old the row was served March prices as live ones:
the status bar's assignment cushion, the portfolio model's spot, a trade's open
legs, the attribution marks (debt TD-02).

One rule, used by all of them: a row older than ``LIVE_QUOTE_MAX_AGE_SEC`` is
not a quote. The threshold is the one the Positions page already applied to
stock prices (``POSITIONS_STK_LIVE_STALE_SEC``, 4 hours), so the bar and the page
agree about what "live" means.

Where a reader needs an underlying price and there is no fresh quote,
``underlying_spot`` falls back to the last daily close from the market-data
plugin -- the same fallback the Positions page uses -- and says so in
``source``, so a close is never presented as a live tick.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, Optional, Tuple

LIVE_QUOTE_MAX_AGE_SEC = float(os.environ.get("POSITIONS_STK_LIVE_STALE_SEC", str(4 * 3600)))

#: Where a position's mark came from -- the ``mark_source`` vocabulary of
#: ``position_snapshot_daily`` and of the attribution rows (TD-140, core 0.51.0).
#: ``quote_live``: a fresh ``contract_quote_live`` row (the window above).
#: ``vendor_eod``: the vendor's close for a dated session -- the snapshot's enriched mark,
#: or for a stock the market-data plugin's daily close. Never presented as a live tick.
MARK_QUOTE_LIVE = "quote_live"
MARK_VENDOR_EOD = "vendor_eod"

SpotSource = Optional[str]  # "live" | "close" | None


def fresh_quote_sql(alias: str) -> str:
    """Join/where condition that keeps only fresh rows of `contract_quote_live` aliased `alias`."""
    return f"{alias}.updated_at >= now() - make_interval(secs => {int(LIVE_QUOTE_MAX_AGE_SEC)})"


def _epoch(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
        f = float(value)
    except (TypeError, ValueError, OSError):
        return None
    return f if math.isfinite(f) else None


def quote_is_fresh(updated_at: Any, now: Optional[float] = None) -> bool:
    """True when the row was written within the live window. No timestamp is not fresh."""
    ts = _epoch(updated_at)
    if ts is None:
        return False
    return ((now if now is not None else time.time()) - ts) <= LIVE_QUOTE_MAX_AGE_SEC


def _positive(*values: Any) -> Optional[float]:
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f) and f > 0:
            return f
    return None


def underlying_spot(
    conn: Any,
    symbol: str,
    *,
    mid: Any = None,
    last: Any = None,
    updated_at: Any = None,
    close_cache: Optional[Dict[str, Optional[Tuple[float, float]]]] = None,
) -> Tuple[Optional[float], SpotSource, Optional[float]]:
    """``(price, source, as_of_epoch)`` for an underlying.

    A fresh live quote (mid, then last) wins. Otherwise the last daily close from
    the market-data plugin, labelled ``"close"``. ``(None, None, None)`` when
    neither exists -- unpriced, which callers count as unknown, never as safe.
    ``close_cache`` dedupes plugin reads across the legs of one request.
    """
    live = _positive(mid, last)
    if live is not None and quote_is_fresh(updated_at):
        return live, "live", _epoch(updated_at)

    sym = (symbol or "").strip().upper()
    if not sym:
        return None, None, None
    if close_cache is not None and sym in close_cache:
        hit = close_cache[sym]
    else:
        from bifrost_core.monitor.reader.market import get_stock_day_fallback_price

        fb = get_stock_day_fallback_price(conn, sym)
        hit = (fb[0], fb[1]) if fb else None
        if close_cache is not None:
            close_cache[sym] = hit
    if hit is None:
        return None, None, None
    return hit[0], "close", hit[1]
