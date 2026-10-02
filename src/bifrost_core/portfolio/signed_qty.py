"""Signed execution quantity: one rule, as SQL and as Python (TD-30, core 0.35.0).

    SELL / SLD / S  ->  -|quantity|
    anything else   ->  +|quantity|
    NULL quantity   ->  NULL (None)

The sign comes from ``side`` alone. How a source stores the number does not matter:
measured on Golden Source (2026-10-02) Flex and journal rows store a sell negative,
TWS rows store it positive, and before 0.35.0 the readers carried five variants of
this rule -- one negated whatever was stored, so Flex and journal sells came back
positive on ``/executions``. ``source`` is accepted so call sites pass a row as it
is; it does not change the answer.

``signed_qty_sql`` and ``signed_qty`` must agree; ``tests/test_signed_qty.py`` runs
the SQL expression in Postgres against the Python function over
source x side x stored sign x NULL.
"""

from __future__ import annotations

import math
from typing import Any, Optional

SELL_SIDES = ("SELL", "SLD", "S")

_SELL_SIDES_SQL = ", ".join(f"'{s}'" for s in SELL_SIDES)


def signed_qty_sql(alias: Optional[str] = "e") -> str:
    """The rule as a SQL expression over ``<alias>.side`` / ``<alias>.quantity``.

    ``alias`` None or "" reads the bare columns. No ``AS``: the caller names it.
    """
    p = f"{alias}." if alias else ""
    return (
        f"CASE WHEN upper(trim(COALESCE({p}side, ''))) IN ({_SELL_SIDES_SQL}) "
        f"THEN -abs({p}quantity) ELSE abs({p}quantity) END"
    )


def is_sell_side(side: Any) -> bool:
    """True for SELL / SLD / S (any case; spaces trimmed as SQL ``trim`` does)."""
    return str(side or "").strip(" ").upper() in SELL_SIDES


def signed_qty(source: Any, side: Any, quantity: Any) -> Optional[float]:
    """The rule in Python. None when ``quantity`` is None or not a number.

    ``source`` is ignored on purpose (see the module docstring).
    """
    del source
    if quantity is None or isinstance(quantity, bool):
        return None
    try:
        q = abs(float(quantity))
    except (TypeError, ValueError):
        return None
    if math.isnan(q):
        return q
    return -q if is_sell_side(side) else q
