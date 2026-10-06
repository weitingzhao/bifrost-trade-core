"""``raw_broker.commissions``: one sign convention and one upsert (TD-114, TD-115).

Stored sign
    IB's statement sign, the way Flex sends ``ibCommission``: a charge is negative, a rebate
    positive. Every other way a commission reaches core is cost-positive: the IB API
    ``CommissionReport`` (TWS events, gateway fills) and the ``commission`` field of
    ``POST`` / ``PUT /executions`` (the Ledger form edits what the readers return). Those
    writers store the negation, :func:`stored_commission`.

Read sign
    Cost-positive for every source: ``-stored`` (:func:`commission_read_sql`), used by
    ``portfolio.reader.executions`` for the ledger, performance and net cash-flow reads.

Before core 0.50.0 the non-Flex writers stored cost-positive and the readers flipped the sign
by the source of the execution row. The commission row is keyed by ``exec_id`` alone, so a
TWS report landing on a Flex-backed fill (or a Ledger edit of one) stored the opposite sign
and read back as a rebate. Rows written that way are restated by the Owner step
bifrost-trade-infra ``scripts/release/db-steps.d/2026-10-06-td114-commission-sign-restate``.

The upsert lives here once; before 0.50.0 it was copied six times across
``portfolio.reader.accounts`` and ``persistence.postgres.postgres_sink``.
"""

from __future__ import annotations

from typing import Any, Optional

from bifrost_core.persistence.postgres.brokerage_tables import GOLDEN_COMMISSIONS

# The only source whose commission is already in the stored sign.
FLEX_SOURCE = "flex_trades"


def _number(value: Any) -> Optional[float]:
    """None and blank strings are "not sent"; anything else must be a number (raises otherwise)."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return float(value)


def stored_commission(value: Any, source: Optional[str]) -> Optional[float]:
    """The stored-sign value for a commission arriving from ``source``.

    ``flex_trades`` values pass through (IB statement sign). Any other source -- TWS
    events, gateway fills, manual and journal rows, API edits -- sends a cost-positive
    value, stored negated. ``None`` / blank stays ``None``.
    """
    n = _number(value)
    if n is None:
        return None
    if (source or "").strip() == FLEX_SOURCE:
        return n
    # 0.0 stays 0.0 (no -0.0 in the table).
    return -n if n else 0.0


def commission_read_sql(alias: str = "c") -> str:
    """Cost-positive commission from the stored sign, for every execution source."""
    return f"-{alias}.commission"


def _zero_to_none(value: Any) -> Any:
    """For the keep-existing upserts: 0 / "0" / "" mean "nothing to say"."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        if float(value) == 0:
            return None
    except (TypeError, ValueError):
        pass
    return value


def upsert_commission(
    cur: Any,
    exec_id: str,
    *,
    commission: Optional[float],
    currency: Any = None,
    realized_pnl: Any = None,
    yield_: Any = None,
    yield_redemption_date: Any = None,
    zero_keeps_existing: bool = True,
) -> None:
    """Insert or merge one ``raw_broker.commissions`` row on ``cur`` (no commit).

    ``commission`` must already be in the stored sign (:func:`stored_commission`).
    A ``None`` field keeps the stored value; a blank currency keeps it too. With
    ``zero_keeps_existing`` (fill imports and IB reports), a zero is also "nothing to
    say": a 7-day re-pull that carries 0 must not erase the value a 1-day pull stored.
    Edits by hand pass ``zero_keeps_existing=False`` so a typed 0 is stored.
    """
    if zero_keeps_existing:
        commission = _zero_to_none(commission)
        realized_pnl = _zero_to_none(realized_pnl)
        yield_ = _zero_to_none(yield_)
        yield_redemption_date = _zero_to_none(yield_redemption_date)
    currency_val = currency if (currency is not None and str(currency).strip()) else None
    t = GOLDEN_COMMISSIONS
    cur.execute(
        f"""
        INSERT INTO {t} (exec_id, commission, currency, realized_pnl, yield_, yield_redemption_date)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (exec_id) DO UPDATE SET
            commission = COALESCE(EXCLUDED.commission, {t}.commission),
            currency = COALESCE(EXCLUDED.currency, {t}.currency),
            realized_pnl = COALESCE(EXCLUDED.realized_pnl, {t}.realized_pnl),
            yield_ = COALESCE(EXCLUDED.yield_, {t}.yield_),
            yield_redemption_date = COALESCE(EXCLUDED.yield_redemption_date, {t}.yield_redemption_date)
        """,
        (exec_id, commission, currency_val, realized_pnl, yield_, yield_redemption_date),
    )
