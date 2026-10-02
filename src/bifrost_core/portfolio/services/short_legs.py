"""Short option legs and what they are priced against.

The shell's status bar wants one thing at a glance: how close the short legs
are to being assigned. The page that answers that properly (Positions) derives
it from ten queries, which is the right cost for a page and the wrong cost for
a bar that is present on every screen in the app.

This is the cheap read behind that bar. It returns the legs and the spot each
one is measured against -- and stops there. The cushion itself is
`(strike - spot) / strike` for a call and `(spot - strike) / strike` for a put,
and the warning line that turns a cushion into "tight" belongs to the trader
and lives in the browser (`useCushionThreshold`). Computing either here would
put a second copy of a rule in a second language, where nothing can catch the
two drifting apart -- so the answer is assembled where the rule already lives,
and this only supplies what the rule needs.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.brokerage_tables import CONTRACT_QUOTE_LIVE, POSITIONS
from bifrost_core.portfolio.quote_freshness import underlying_spot

logger = logging.getLogger(__name__)


_SQL = f"""
    SELECT
        p.account_id,
        p.symbol,
        p.expiry,
        p.strike,
        p.option_right,
        p.position AS qty,
        p.contract_key,
        stk.mid  AS stk_mid,
        stk.last AS stk_last,
        stk.updated_at AS stk_updated_at
    FROM {POSITIONS} p
    LEFT JOIN LATERAL (
        SELECT q.mid, q.last, q.updated_at
        FROM {CONTRACT_QUOTE_LIVE} q
        WHERE q.symbol = p.symbol AND q.sec_type = 'STK'
        ORDER BY q.updated_at DESC NULLS LAST
        LIMIT 1
    ) stk ON TRUE
    WHERE p.sec_type = 'OPT'
      AND p.position < 0
      AND (%(accounts)s::text[] IS NULL OR p.account_id = ANY(%(accounts)s::text[]))
    ORDER BY p.symbol, p.expiry, p.strike
"""


def get_short_option_legs(
    conn: Any,
    account_ids: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Every short option leg, with the price its underlying is measured against.

    `spot` is the underlying's live quote when one was written within the live
    window, else its last daily close (`spot_source` says which, `spot_as_of`
    when). It is null only when neither exists; the caller counts that as
    unpriced, never as safe. A live row older than the window is not used:
    under D10 the daemon writes none, and the table's March rows were being
    served as today's spot (debt TD-02).
    """
    accounts = list(account_ids) if account_ids else None
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(_SQL, {"accounts": accounts})
        rows = [dict(r) for r in cur.fetchall()]

    legs: List[Dict[str, Any]] = []
    closes: Dict[str, Any] = {}
    for r in rows:
        spot, source, as_of = underlying_spot(
            conn,
            r.get("symbol") or "",
            mid=r.get("stk_mid"),
            last=r.get("stk_last"),
            updated_at=r.get("stk_updated_at"),
            close_cache=closes,
        )
        legs.append(
            {
                "account_id": r.get("account_id"),
                "symbol": (r.get("symbol") or "").strip().upper(),
                "expiry": r.get("expiry"),
                "strike": float(r["strike"]) if r.get("strike") is not None else None,
                "right": (r.get("option_right") or "").strip().upper() or None,
                "qty": int(r["qty"]) if r.get("qty") is not None else 0,
                "contract_key": r.get("contract_key"),
                "spot": spot,
                "spot_source": source,
                "spot_as_of": as_of,
            }
        )
    return legs
