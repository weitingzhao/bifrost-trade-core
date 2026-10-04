"""Where a strategy instance stands, read from its own option fills (TD-43, core 0.41.0).

One rule for every reader -- Trading > Rules, Review, Risk > Limits and the research MCP
used to hold three different ones. It is the Ledger's (frontend ``buildOptExecutionGroups``
and Review's ``instanceOf``):

- The instance's OPT fills are grouped per contract (``contract_key``; when it is empty,
  ``SYMBOL|OPT|YYYYMMDD|STRIKE|R`` from the fill's parts). A fill attributed whole counts
  its quantity; a split fill counts the instance's share (``split_quantity``). Buys
  (BUY / BOT / B) add, sells (SELL / SLD / S) subtract.
- A leg is flat when ``|net| < 1e-9``; its last fill date (``trade_date``, else the fill
  time's New York date) is the day it went flat.

States:

- ``no_fills``: no option fill is attributed to it. Nothing has happened yet -- not closed.
- ``open``: a leg is still open and at least one open leg has not expired.
- ``expired``: every open leg is past its expiry (strictly before today) with no closing
  fill -- over, booked by nobody. Review reads it as expired worthless. It counts as
  closed: ``closed_on`` is the last of those expiries.
- ``closed``: every leg is flat; ``closed_on`` is the last day a leg went flat.

``is_closed(state)`` is the one answer to "does it still take up room": ``expired`` and
``closed`` do, ``open`` and ``no_fills`` do not count as closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from bifrost_core.persistence.postgres.brokerage_tables import EXECUTIONS, TRADE_EXECUTION

INSTANCE_STATES = ("no_fills", "open", "expired", "closed")
CLOSED_STATES = ("expired", "closed")
NET_QTY_EPS = 1e-9
_NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class InstanceLeg:
    """One contract of an instance: its net quantity, its expiry and the last day it traded."""

    contract_key: str
    expiry: Optional[date]
    net_qty: float
    last_fill_on: Optional[date]

    @property
    def is_open(self) -> bool:
        return abs(self.net_qty) >= NET_QTY_EPS


def is_closed(state: Optional[str]) -> bool:
    return state in CLOSED_STATES


def today_new_york(now: Optional[datetime] = None) -> date:
    """The trading calendar's today: the New York date."""
    moment = now or datetime.now(_NEW_YORK)
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(_NEW_YORK).date()


def parse_expiry(raw: Any) -> Optional[date]:
    """``20261016`` or ``2026-10-16`` -> date; anything else -> None (never expires)."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def derive_state(legs: Iterable[InstanceLeg], today: date) -> Tuple[str, Optional[date]]:
    """``(state, closed_on)`` for one instance's legs. ``closed_on`` is None unless it is closed."""
    legs = list(legs)
    if not legs:
        return "no_fills", None
    open_legs = [leg for leg in legs if leg.is_open]
    if not open_legs:
        flat = [leg.last_fill_on for leg in legs if leg.last_fill_on is not None]
        return "closed", max(flat) if flat else None
    expiries = [leg.expiry for leg in open_legs]
    if all(e is not None and e < today for e in expiries):
        return "expired", max(e for e in expiries if e is not None)
    return "open", None


# One row per (instance, contract) over this env's attribution: whole fills count their
# quantity, split fills the instance's share. brokerage.executions has one row per fill (the
# Flex row, else the TWS row, plus journal rows), joined on the fill's (account_id, exec_id).
_LEGS_SQL = f"""
    WITH fills AS (
        SELECT sie.trade_id AS sid,
               COALESCE(
                   NULLIF(trim(e.contract_key), ''),
                   split_part(COALESCE(e.symbol, ''), ' ', 1) || '|OPT|'
                       || replace(COALESCE(e.expiry, ''), '-', '') || '|'
                       || COALESCE(e.strike, 0)::text || '|'
                       || upper(left(COALESCE(NULLIF(e.option_right, ''), 'C'), 1))
               ) AS contract_key,
               e.expiry,
               CASE WHEN upper(trim(COALESCE(e.side, ''))) IN ('BUY', 'BOT', 'B') THEN 1
                    WHEN upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S') THEN -1
                    ELSE 0 END
                 * abs(COALESCE(sie.split_quantity::double precision, e.quantity, 0)) AS signed_qty,
               COALESCE(e.trade_date, (e.exec_time AT TIME ZONE 'America/New_York')::date) AS fill_on
        FROM {TRADE_EXECUTION} sie
        JOIN {EXECUTIONS} e ON e.account_id = sie.account_id AND e.exec_id = sie.exec_id
        WHERE upper(trim(COALESCE(e.sec_type, ''))) = 'OPT'
          AND (%(ids)s::bigint[] IS NULL OR sie.trade_id = ANY(%(ids)s::bigint[]))
    )
    SELECT sid, contract_key, min(expiry) AS expiry, sum(signed_qty) AS net_qty, max(fill_on) AS last_fill_on
    FROM fills
    GROUP BY sid, contract_key
"""


def read_instance_legs(cur: Any, strategy_instance_ids: Optional[List[int]] = None) -> Dict[int, List[InstanceLeg]]:
    """Every attributed instance's option legs, or only those of ``strategy_instance_ids``."""
    cur.execute(_LEGS_SQL, {"ids": list(strategy_instance_ids) if strategy_instance_ids else None})
    out: Dict[int, List[InstanceLeg]] = {}
    for row in cur.fetchall():
        if isinstance(row, dict):
            sid, ck, expiry, net, last = (
                row["sid"], row["contract_key"], row["expiry"], row["net_qty"], row["last_fill_on"]
            )
        else:
            sid, ck, expiry, net, last = row
        out.setdefault(int(sid), []).append(
            InstanceLeg(
                contract_key=str(ck),
                expiry=parse_expiry(expiry),
                net_qty=float(net or 0.0),
                last_fill_on=last,
            )
        )
    return out


def instance_states(
    cur: Any, strategy_instance_ids: Iterable[int], today: Optional[date] = None
) -> Dict[int, Tuple[str, Optional[date]]]:
    """``{strategy_instance_id: (state, closed_on)}`` for the given instances."""
    ids = [int(i) for i in strategy_instance_ids]
    if not ids:
        return {}
    legs = read_instance_legs(cur, ids)
    day = today or today_new_york()
    return {sid: derive_state(legs.get(sid, []), day) for sid in ids}
