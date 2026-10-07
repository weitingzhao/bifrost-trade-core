"""Read the daily book snapshots (TD-138 / TD-139, core 0.54.0).

``position_snapshot_daily`` and ``account_nav_daily`` are written by the nightly job
(``bifrost_core.portfolio.snapshot``); this is their reader. Read-only: no write, no commit.

Three reads:

* :func:`nav_history` -- one row per session and account. A row whose ``account_updated_at`` is
  before that session's close (``session_closes_at``: 16:00 New York or the NYSE early close) is
  not a closing balance -- its TWS was not connected at the close -- and is left out of ``items``
  and listed in ``dropped``. Core 0.52.0 stopped writing such rows; rows written before it can
  still be intraday reads, and this keeps them out of every curve and return.
* :func:`position_snapshots` -- the positions of each session, every row with its
  ``greeks_quality``, and a rollup per session and ``trade_id`` (SNAPSHOT-SPEC §2: the part of a
  position no trade explains is ``trade_id`` null and is never spread over the trades).
* :func:`pnl_attribution` -- each session against the session before it (SNAPSHOT-SPEC §2 rule 3:
  ``(t) - (t-1)``; a session whose prior session was not captured has no reading, it is never
  differenced against an older day). Per row: the held P&L of the position as it stood at the
  prior close, and its delta / gamma / vega / theta parts from the prior close's vendor Greeks;
  ``unexplained`` is the held P&L less the four. Rolled up per trade, per underlying and in total.

``greeks_quality`` (TD-139, derived on read; no column):

* ``vendor``   -- the vendor's Greeks of that session, all five values present, and the mark is
  the vendor's session close;
* ``degraded`` -- Greeks present but not that session's (``greeks_asof`` on another New York
  date, or none), one of gamma / vega / theta / iv missing, or a mark that is not the vendor
  close (a live quote at capture, or none);
* ``missing``  -- no delta: the vendor had no row for the contract.

Stock rows carry ``greeks_quality`` null: a share has delta 1 and nothing else to read.

Units, as the vendor (Polygon, via the market-data plugin) sends them: ``iv`` a fraction,
``vega`` per one volatility point (0.01 of iv), ``theta`` per calendar day; an option row's
quantities are contracts of 100 shares (``OPTION_MULTIPLIER``).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader.errors import ReadFailed
from bifrost_core.persistence.postgres.snapshot_ddl import ACCOUNT_NAV_DAILY, POSITION_SNAPSHOT_DAILY
from bifrost_core.portfolio.quote_freshness import MARK_VENDOR_EOD
# One number and one timestamp rule with the writer: its NaN / inf -> None and isoformat helpers.
from bifrost_core.portfolio.snapshot.daily import _finite, _iso, session_closes_at

logger = logging.getLogger(__name__)

GREEKS_VENDOR = "vendor"
GREEKS_DEGRADED = "degraded"
GREEKS_MISSING = "missing"
GREEKS_QUALITIES: Tuple[str, ...] = (GREEKS_VENDOR, GREEKS_DEGRADED, GREEKS_MISSING)

#: Shares per option contract. The table keeps no multiplier; every option this book trades is 100.
OPTION_MULTIPLIER = 100.0

#: The longest range one read answers, in calendar days (a guard, not a business rule).
MAX_RANGE_DAYS = 800

# Attribution row status.
ATTR_OK = "ok"  # held through the session: both closes on file
ATTR_OPENED = "opened_in_session"  # no row at the prior close: its day is a fill, read on Performance
ATTR_CLOSED = "closed_in_session"  # no row at this close: closed or rolled during the session
ATTR_NO_MARK = "no_mark"  # held through, but a mark (or the underlying's close) is missing on one side

# Session status.
SESSION_OK = "ok"
SESSION_NO_PRIOR = "no_prior_snapshot"


def _is_option(row: Mapping[str, Any]) -> bool:
    return (row.get("sec_type") or "").strip().upper() == "OPT"


def multiplier(row: Mapping[str, Any]) -> float:
    return OPTION_MULTIPLIER if _is_option(row) else 1.0


# --------------------------------------------------------------------------- mark check


def mark_below_intrinsic(row: Mapping[str, Any]) -> Optional[bool]:
    """True when an option's mark is under its intrinsic value at the underlying's close.

    A vendor close is the last trade, and a thin contract's last trade can be days old: a mark
    under intrinsic cannot be that session's price (SNAPSHOT-SPEC §3, "mark anomaly"). None when
    it cannot be judged (a stock, or a mark / close / strike / right missing).
    """
    if not _is_option(row):
        return None
    m, s, k = _finite(row.get("mark")), _finite(row.get("underlying_close")), _finite(row.get("strike"))
    right = (row.get("option_right") or "").strip().upper()[:1]
    if m is None or s is None or k is None or right not in ("C", "P"):
        return None
    intrinsic = max(0.0, s - k) if right == "C" else max(0.0, k - s)
    return m < intrinsic - 0.01


# --------------------------------------------------------------------------- greeks quality


def greeks_quality(row: Mapping[str, Any], session_date: date) -> Tuple[Optional[str], Optional[str]]:
    """``(quality, reason)`` for an option row of ``session_date``; ``(None, None)`` for a stock.

    ``row["greeks_session"]`` is the New York date of ``greeks_asof`` (the reader computes it in
    SQL: the container may lack tzdata).
    """
    if not _is_option(row):
        return None, None
    if _finite(row.get("delta")) is None:
        return GREEKS_MISSING, "the vendor had no Greeks for this contract at the session's close"
    absent = [k for k in ("gamma", "vega", "theta", "iv") if _finite(row.get(k)) is None]
    if absent:
        return GREEKS_DEGRADED, f"delta present, {' / '.join(absent)} missing"
    gs = row.get("greeks_session")
    if gs is None:
        return GREEKS_DEGRADED, "the Greeks carry no as-of time"
    if gs != session_date:
        return GREEKS_DEGRADED, f"the Greeks are as of {_iso(gs)}, not this session"
    if row.get("mark_source") != MARK_VENDOR_EOD:
        src = row.get("mark_source") or "no mark"
        return GREEKS_DEGRADED, f"the mark is {src}, not the vendor's session close the Greeks belong to"
    return GREEKS_VENDOR, None


def _quality_counts(qualities: Iterable[Optional[str]]) -> Dict[str, int]:
    out = {q: 0 for q in GREEKS_QUALITIES}
    for q in qualities:
        if q in out:
            out[q] += 1
    return out


# --------------------------------------------------------------------------- inputs


def _date_range(from_date: Optional[date], to_date: Optional[date]) -> None:
    if from_date and to_date:
        if from_date > to_date:
            raise ValueError("from_date is after to_date")
        if (to_date - from_date).days > MAX_RANGE_DAYS:
            raise ValueError(f"the range is longer than {MAX_RANGE_DAYS} days")


def _fetch(conn: Any, sql: str, params: Any) -> List[Dict[str, Any]]:
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except psycopg2.Error as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("snapshot read failed: %s", e)
        raise ReadFailed(f"the daily snapshot could not be read: {e.__class__.__name__}") from e


def _scalar(conn: Any, sql: str, params: Any) -> Any:
    rows = _fetch(conn, sql, params)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def _closes(conn: Any, dates: Iterable[date]) -> Dict[date, Any]:
    try:
        return session_closes_at(conn, dates)
    except psycopg2.Error as e:
        raise ReadFailed(f"the session close could not be read: {e.__class__.__name__}") from e


def _closed_holidays(conn: Any, lo: date, hi: date) -> Set[date]:
    """Full-day NYSE holidays in ``[lo, hi]`` from ``market.us_market_holiday``; empty when the
    calendar is not reachable (a weekday is then taken for a session, as the writer does)."""
    if _scalar(conn, "SELECT to_regclass('market.us_market_holiday') IS NOT NULL AS ok", None) is not True:
        return set()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT holiday_date FROM market.us_market_holiday WHERE holiday_date BETWEEN %s AND %s "
                "AND upper(exchange) = 'NYSE' AND lower(status) = 'closed'",
                (lo, hi),
            )
            return {d for (d,) in cur.fetchall()}
    except psycopg2.Error as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("holiday lookup failed (%s); weekdays are taken for sessions", e)
        return set()


def previous_session(d: date, closed: Set[date]) -> date:
    """The NYSE session before ``d``: the last weekday before it that is not a full-day holiday."""
    p = d - timedelta(days=1)
    while p.weekday() >= 5 or p in closed:
        p -= timedelta(days=1)
    return p


# --------------------------------------------------------------------------- NAV history

_NAV_SQL = f"""
SELECT snapshot_date, account_id, net_liquidation, total_cash, buying_power, cushion,
       excess_liquidity, maint_margin_req, account_updated_at, captured_at
FROM {ACCOUNT_NAV_DAILY}
WHERE (%(account_id)s::text IS NULL OR account_id = %(account_id)s)
  AND (%(from_date)s::date IS NULL OR snapshot_date >= %(from_date)s)
  AND (%(to_date)s::date IS NULL OR snapshot_date <= %(to_date)s)
ORDER BY snapshot_date, account_id
"""


def nav_history(
    conn: Any,
    *,
    account_id: Optional[str] = None,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Closing NAV per session and account; rows read before the close are in ``dropped``.

    ``{"items": [...], "dropped": [...], "sessions": [iso dates with at least one kept row]}``.
    """
    _date_range(from_date, to_date)
    acct = (account_id or "").strip() or None
    rows = _fetch(conn, _NAV_SQL, {"account_id": acct, "from_date": from_date, "to_date": to_date})
    closes = _closes(conn, {r["snapshot_date"] for r in rows})
    items: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for r in rows:
        close_at = closes.get(r["snapshot_date"])
        updated = r.get("account_updated_at")
        base = {
            "snapshot_date": _iso(r["snapshot_date"]),
            "account_id": r["account_id"],
            "account_updated_at": _iso(updated),
            "session_close": _iso(close_at),
        }
        if updated is None or close_at is None or updated < close_at:
            dropped.append({**base, "reason": "account read before the session's close (not a closing balance)"})
            continue
        items.append(
            {
                **base,
                "net_liquidation": _finite(r.get("net_liquidation")),
                "total_cash": _finite(r.get("total_cash")),
                "buying_power": _finite(r.get("buying_power")),
                "cushion": _finite(r.get("cushion")),
                "excess_liquidity": _finite(r.get("excess_liquidity")),
                "maint_margin_req": _finite(r.get("maint_margin_req")),
                "captured_at": _iso(r.get("captured_at")),
            }
        )
    sessions = sorted({i["snapshot_date"] for i in items})
    return {"items": items, "dropped": dropped, "sessions": sessions}


# --------------------------------------------------------------------------- positions

_POS_COLUMNS = """
    snapshot_date, account_id, contract_key, trade_id, symbol, sec_type, expiry, strike,
    option_right, position_qty, trade_qty, avg_cost, mark, mark_source, underlying_close,
    delta, gamma, vega, theta, iv, greeks_asof,
    (greeks_asof AT TIME ZONE 'America/New_York')::date AS greeks_session,
    positions_updated_at, captured_at
"""

_POS_SQL = f"""
SELECT {_POS_COLUMNS}
FROM {POSITION_SNAPSHOT_DAILY}
WHERE snapshot_date = ANY(%(dates)s::date[])
  AND (%(account_id)s::text IS NULL OR account_id = %(account_id)s)
ORDER BY snapshot_date, account_id, symbol, contract_key, trade_id NULLS LAST
"""

_POS_DATES_SQL = f"""
SELECT DISTINCT snapshot_date FROM {POSITION_SNAPSHOT_DAILY}
WHERE (%(account_id)s::text IS NULL OR account_id = %(account_id)s)
  AND (%(from_date)s::date IS NULL OR snapshot_date >= %(from_date)s)
  AND (%(to_date)s::date IS NULL OR snapshot_date <= %(to_date)s)
ORDER BY snapshot_date
"""

_FRESH_ACCOUNTS_SQL = f"""
SELECT snapshot_date, account_id, account_updated_at FROM {ACCOUNT_NAV_DAILY}
WHERE snapshot_date = ANY(%(dates)s::date[])
"""


def _session_dates(
    conn: Any, account_id: Optional[str], from_date: Optional[date], to_date: Optional[date]
) -> List[date]:
    """The captured sessions in the range; with no bound given, the latest one only."""
    rows = _fetch(conn, _POS_DATES_SQL, {"account_id": account_id, "from_date": from_date, "to_date": to_date})
    dates = [r["snapshot_date"] for r in rows]
    if from_date is None and to_date is None:
        return dates[-1:]
    return dates


def _fresh_at_close(conn: Any, dates: List[date]) -> Dict[Tuple[date, str], bool]:
    """(session, account) -> whether the account's NAV row was read at or after the close."""
    if not dates:
        return {}
    rows = _fetch(conn, _FRESH_ACCOUNTS_SQL, {"dates": dates})
    closes = _closes(conn, dates)
    out: Dict[Tuple[date, str], bool] = {}
    for r in rows:
        close_at = closes.get(r["snapshot_date"])
        u = r.get("account_updated_at")
        out[(r["snapshot_date"], r["account_id"])] = bool(u is not None and close_at is not None and u >= close_at)
    return out


def _position_rows(conn: Any, dates: List[date], account_id: Optional[str]) -> List[Dict[str, Any]]:
    if not dates:
        return []
    rows = _fetch(conn, _POS_SQL, {"dates": dates, "account_id": account_id})
    for r in rows:
        q, why = greeks_quality(r, r["snapshot_date"])
        r["greeks_quality"] = q
        r["greeks_quality_reason"] = why
    return rows


def _position_item(r: Mapping[str, Any], fresh: Optional[bool]) -> Dict[str, Any]:
    qty = _finite(r.get("trade_qty")) or 0.0
    mark = _finite(r.get("mark"))
    mult = multiplier(r)
    delta = 1.0 if not _is_option(r) else _finite(r.get("delta"))
    return {
        "snapshot_date": _iso(r["snapshot_date"]),
        "account_id": r["account_id"],
        "contract_key": r["contract_key"],
        "trade_id": r.get("trade_id"),
        "symbol": r.get("symbol"),
        "sec_type": r.get("sec_type"),
        "expiry": _iso(r.get("expiry")),
        "strike": _finite(r.get("strike")),
        "option_right": r.get("option_right"),
        "position_qty": _finite(r.get("position_qty")),
        "trade_qty": qty,
        "multiplier": mult,
        "avg_cost": _finite(r.get("avg_cost")),
        "mark": mark,
        "mark_source": r.get("mark_source"),
        "market_value": None if mark is None else qty * mult * mark,
        "underlying_close": _finite(r.get("underlying_close")),
        "delta": _finite(r.get("delta")),
        "gamma": _finite(r.get("gamma")),
        "vega": _finite(r.get("vega")),
        "theta": _finite(r.get("theta")),
        "iv": _finite(r.get("iv")),
        "delta_shares": None if delta is None else qty * mult * delta,
        "greeks_asof": _iso(r.get("greeks_asof")),
        "greeks_quality": r.get("greeks_quality"),
        "greeks_quality_reason": r.get("greeks_quality_reason"),
        "mark_below_intrinsic": mark_below_intrinsic(r),
        "account_fresh_at_close": fresh,
        "positions_updated_at": _iso(r.get("positions_updated_at")),
        "captured_at": _iso(r.get("captured_at")),
    }


def _trade_rollup(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per session and trade_id (null = Unattributed): legs, names, value, delta and Greek quality."""
    groups: Dict[Tuple[str, Optional[int]], List[Dict[str, Any]]] = {}
    for i in items:
        groups.setdefault((i["snapshot_date"], i["trade_id"]), []).append(i)
    out: List[Dict[str, Any]] = []
    for (d, tid), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] is None, kv[0][1] or 0)):
        priced = [r for r in rows if r["market_value"] is not None]
        with_delta = [r for r in rows if r["delta_shares"] is not None]
        out.append(
            {
                "snapshot_date": d,
                "trade_id": tid,
                "rows": len(rows),
                "symbols": sorted({(r["symbol"] or "").strip().upper() for r in rows if r.get("symbol")}),
                "accounts": sorted({r["account_id"] for r in rows}),
                "market_value": sum(r["market_value"] for r in priced) if priced else None,
                "unpriced_rows": len(rows) - len(priced),
                "delta_shares": sum(r["delta_shares"] for r in with_delta) if with_delta else None,
                "rows_without_delta": len(rows) - len(with_delta),
                "greeks_quality": _quality_counts(r["greeks_quality"] for r in rows),
            }
        )
    return out


def position_snapshots(
    conn: Any,
    *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    account_id: Optional[str] = None,
    trade_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Positions per session (the latest one when no bound is given) with a per-trade rollup.

    ``{"items": [...], "trades": [...], "sessions": [...], "greeks_quality": {session: counts}}``.
    ``account_fresh_at_close`` is false for a row whose account's NAV row was read before the
    close (rows written before core 0.52.0), null when the session has no NAV row for the account.
    """
    _date_range(from_date, to_date)
    acct = (account_id or "").strip() or None
    dates = _session_dates(conn, acct, from_date, to_date)
    rows = _position_rows(conn, dates, acct)
    if trade_id is not None:
        rows = [r for r in rows if r.get("trade_id") == trade_id]
    fresh = _fresh_at_close(conn, dates)
    items = [_position_item(r, fresh.get((r["snapshot_date"], r["account_id"]))) for r in rows]
    by_session: Dict[str, List[Optional[str]]] = {_iso(d): [] for d in dates}
    for i in items:
        by_session.setdefault(i["snapshot_date"], []).append(i["greeks_quality"])
    return {
        "items": items,
        "trades": _trade_rollup(items),
        "sessions": [_iso(d) for d in dates],
        "greeks_quality": {d: _quality_counts(q) for d, q in by_session.items()},
    }


# --------------------------------------------------------------------------- attribution

_PARTS = ("held_pnl", "delta_pnl", "gamma_pnl", "vega_pnl", "theta_pnl", "unexplained")


def attribute_row(
    prior: Optional[Mapping[str, Any]],
    current: Optional[Mapping[str, Any]],
    days: int,
) -> Dict[str, Any]:
    """One position across one session: its held P&L and the four parts from the prior close.

    ``prior`` / ``current`` are snapshot rows (with ``greeks_quality``) of the prior and this
    session; either may be None. Values are None where they cannot be read -- never zero.
    """
    out: Dict[str, Any] = {k: None for k in _PARTS}
    out["greeks_quality"] = prior.get("greeks_quality") if prior else None
    if prior is None:
        out["status"] = ATTR_OPENED
        return out
    if current is None:
        out["status"] = ATTR_CLOSED
        return out
    qty = (_finite(prior.get("trade_qty")) or 0.0) * multiplier(prior)
    m0, m1 = _finite(prior.get("mark")), _finite(current.get("mark"))
    s0, s1 = _finite(prior.get("underlying_close")), _finite(current.get("underlying_close"))
    if not _is_option(prior):
        # A share: its own close is the underlying's.
        s0 = s0 if s0 is not None else m0
        s1 = s1 if s1 is not None else m1
    if m0 is None or m1 is None or s0 is None or s1 is None:
        out["status"] = ATTR_NO_MARK
        if m0 is not None and m1 is not None:
            out["held_pnl"] = qty * (m1 - m0)
        return out
    out["status"] = ATTR_OK
    held = qty * (m1 - m0)
    ds = s1 - s0
    out["held_pnl"] = held
    if not _is_option(prior):
        out.update(delta_pnl=qty * ds, gamma_pnl=0.0, vega_pnl=0.0, theta_pnl=0.0)
    else:
        d, g, v, t = (_finite(prior.get(k)) for k in ("delta", "gamma", "vega", "theta"))
        iv0, iv1 = _finite(prior.get("iv")), _finite(current.get("iv"))
        if d is not None:
            out["delta_pnl"] = qty * d * ds
        if g is not None:
            out["gamma_pnl"] = qty * 0.5 * g * ds * ds
        if v is not None and iv0 is not None and iv1 is not None:
            out["vega_pnl"] = qty * v * (iv1 - iv0) * 100.0
        if t is not None:
            out["theta_pnl"] = qty * t * days
    parts = [out[k] for k in ("delta_pnl", "gamma_pnl", "vega_pnl", "theta_pnl")]
    if all(p is not None for p in parts):
        out["unexplained"] = held - sum(parts)
    return out


def _empty_sums() -> Dict[str, Any]:
    return {
        **{k: 0.0 for k in _PARTS},
        "rows": 0,
        "read_rows": 0,
        "unread_rows": 0,
        "unread_held_pnl": 0.0,
        "mark_anomaly_rows": 0,
        "mark_anomaly_unexplained": 0.0,
        "greeks_quality": {q: 0 for q in GREEKS_QUALITIES},
        "status": {s: 0 for s in (ATTR_OK, ATTR_OPENED, ATTR_CLOSED, ATTR_NO_MARK)},
    }


def _add(sums: Dict[str, Any], row: Mapping[str, Any]) -> None:
    """Add a row: a fully read row to the six sums (so they keep the identity), any other to the
    unread count (with the held P&L it does have)."""
    sums["rows"] += 1
    sums["status"][row["status"]] = sums["status"].get(row["status"], 0) + 1
    if row.get("greeks_quality") in sums["greeks_quality"]:
        sums["greeks_quality"][row["greeks_quality"]] += 1
    if row.get("unexplained") is not None:
        sums["read_rows"] += 1
        for k in _PARTS:
            sums[k] += row[k]
        if row.get("mark_below_intrinsic"):
            sums["mark_anomaly_rows"] += 1
            sums["mark_anomaly_unexplained"] += row["unexplained"]
    else:
        sums["unread_rows"] += 1
        if row.get("held_pnl") is not None:
            sums["unread_held_pnl"] += row["held_pnl"]


def _key(r: Mapping[str, Any]) -> Tuple[str, str, Optional[int]]:
    return (r["account_id"], r["contract_key"], r.get("trade_id"))


def pnl_attribution(
    conn: Any,
    *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    account_id: Optional[str] = None,
    trade_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Each captured session in the range (the latest when no bound is given) against its prior
    session, per position; rolled up per session, per trade, per underlying and in total.

    A session whose prior NYSE session has no snapshot reads ``no_prior_snapshot`` and adds no row.
    """
    _date_range(from_date, to_date)
    acct = (account_id or "").strip() or None
    dates = _session_dates(conn, acct, from_date, to_date)
    if not dates:
        return {"items": [], "sessions": [], "by_trade": [], "by_symbol": [], "totals": _empty_sums()}
    closed = _closed_holidays(conn, dates[0] - timedelta(days=14), dates[-1])
    priors = {d: previous_session(d, closed) for d in dates}
    stored = {r["snapshot_date"] for r in _fetch(conn, _POS_DATES_SQL, {"account_id": acct, "from_date": None, "to_date": None})}
    need = sorted({*dates, *(p for p in priors.values() if p in stored)})
    rows = _position_rows(conn, need, acct)
    by_date: Dict[date, Dict[Tuple[str, str, Optional[int]], Dict[str, Any]]] = {}
    for r in rows:
        by_date.setdefault(r["snapshot_date"], {})[_key(r)] = r

    items: List[Dict[str, Any]] = []
    sessions: List[Dict[str, Any]] = []
    for d in dates:
        p = priors[d]
        if p not in stored:
            sessions.append(
                {"snapshot_date": _iso(d), "prior_date": _iso(p), "status": SESSION_NO_PRIOR, "totals": None}
            )
            continue
        prev, cur = by_date.get(p, {}), by_date.get(d, {})
        days = (d - p).days
        sums = _empty_sums()
        for k in sorted(set(prev) | set(cur), key=lambda k: (k[0], k[1], k[2] is None, k[2] or 0)):
            a, b = prev.get(k), cur.get(k)
            if trade_id is not None and k[2] != trade_id:
                continue
            meta = a or b or {}
            row = {
                "snapshot_date": _iso(d),
                "prior_date": _iso(p),
                "account_id": k[0],
                "contract_key": k[1],
                "trade_id": k[2],
                "symbol": meta.get("symbol"),
                "sec_type": meta.get("sec_type"),
                "expiry": _iso(meta.get("expiry")),
                "strike": _finite(meta.get("strike")),
                "option_right": meta.get("option_right"),
                "qty_at_prior_close": _finite(a.get("trade_qty")) if a else None,
                "multiplier": multiplier(meta),
                **attribute_row(a, b, days),
            }
            if row["greeks_quality"] is not None:
                row["greeks_quality_reason"] = a.get("greeks_quality_reason") if a else None
            # A mark under intrinsic on either side makes the held P&L (and so the residual) a
            # stale-print artefact, not a reading (SNAPSHOT-SPEC §3).
            row["mark_below_intrinsic"] = [
                side for side, r in (("prior", a), ("current", b)) if r is not None and mark_below_intrinsic(r)
            ]
            items.append(row)
            _add(sums, row)
        sessions.append({"snapshot_date": _iso(d), "prior_date": _iso(p), "status": SESSION_OK, "days": days, "totals": sums})

    def rollup(key_of) -> List[Dict[str, Any]]:
        groups: Dict[Any, Dict[str, Any]] = {}
        for r in items:
            groups.setdefault(key_of(r), _empty_sums())
            _add(groups[key_of(r)], r)
        return [{"key": k, **v} for k, v in groups.items()]

    by_trade = [
        {"trade_id": g.pop("key"), **g}
        for g in sorted(rollup(lambda r: r["trade_id"]), key=lambda g: (g["key"] is None, g["key"] or 0))
    ]
    by_symbol = [
        {"symbol": g.pop("key"), **g}
        for g in sorted(rollup(lambda r: (r.get("symbol") or "").strip().upper() or None), key=lambda g: g["key"] or "")
    ]
    totals = _empty_sums()
    for r in items:
        _add(totals, r)
    return {"items": items, "sessions": sessions, "by_trade": by_trade, "by_symbol": by_symbol, "totals": totals}


__all__ = [
    "ATTR_CLOSED",
    "ATTR_NO_MARK",
    "ATTR_OK",
    "ATTR_OPENED",
    "GREEKS_DEGRADED",
    "GREEKS_MISSING",
    "GREEKS_QUALITIES",
    "GREEKS_VENDOR",
    "OPTION_MULTIPLIER",
    "SESSION_NO_PRIOR",
    "SESSION_OK",
    "attribute_row",
    "greeks_quality",
    "mark_below_intrinsic",
    "multiplier",
    "nav_history",
    "pnl_attribution",
    "position_snapshots",
    "previous_session",
]
