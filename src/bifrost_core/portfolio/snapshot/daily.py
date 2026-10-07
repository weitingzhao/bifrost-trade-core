"""The nightly book snapshot (W4, core 0.48.0): positions per trade and account NAV.

Two steps, both idempotent for a session date:

* ``capture`` -- right after the close. Reads the broker's current book (``brokerage.positions``
  and ``brokerage.account``, FDW to Golden Source) and the trade attribution of each position,
  and writes ``position_snapshot_daily`` / ``account_nav_daily``. This is the part that cannot be
  recovered later: the broker tables hold the current state only. Per account: an account whose
  ``brokerage.account.updated_at`` is older than the session's close is stale (its TWS was not
  connected at the close) and is skipped, NAV and positions alike, and listed in the result; an
  account that already has its NAV row for the date is kept as written. A later run the same
  evening (the CronJob's ``all``) therefore picks up only the accounts that were stale, once
  their broker sync is back after the close (core 0.52.0).
* ``enrich`` -- later the same evening. Fills the vendor EOD values the market-data plugin has
  for that session (``/options/snapshots?as_of=`` is the 16:00 anchor of the day, whenever it is
  read; ``/stocks/db/bars/benchmark`` the daily close): Greeks, IV, the option's close as the
  mark where capture had no live quote, and the underlying close. Only NULLs are filled.

Read-only towards the broker and the plugin; writes only the two snapshot tables. No order, no
Redis, no daemon (D10).
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.brokerage_tables import ACCOUNT, POSITIONS
from bifrost_core.persistence.postgres.snapshot_ddl import ACCOUNT_NAV_DAILY, POSITION_SNAPSHOT_DAILY
from bifrost_core.portfolio.quote_freshness import MARK_QUOTE_LIVE, MARK_VENDOR_EOD  # mark_source values

logger = logging.getLogger(__name__)

#: Below this a split remainder is float noise, not an unattributed position.
QTY_EPS = 1e-6


class SnapshotError(RuntimeError):
    """The snapshot could not be taken; the job exits non-zero so the CronJob shows Failed."""


# --------------------------------------------------------------------------- helpers


def _finite(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_expiry(value: Any) -> Optional[date]:
    """``20261017`` / ``2026-10-17`` -> date; anything else -> None."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8:
        return None
    try:
        return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


def vendor_option_ticker(symbol: Any, expiry: Any, strike: Any, right: Any) -> Optional[str]:
    """The vendor's (Polygon) option ticker, ``O:AAPL261017C00150000``; None when a part is missing."""
    sym = (str(symbol or "")).strip().upper()
    exp = parse_expiry(expiry)
    k = _finite(strike)
    r = (str(right or "")).strip().upper()[:1]
    if not sym or exp is None or k is None or r not in ("C", "P"):
        return None
    return f"O:{sym}{exp.strftime('%y%m%d')}{r}{int(round(k * 1000)):08d}"


def split_rows(attribution: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Attribution rows -> snapshot rows: one per (account, contract, trade).

    ``open_qty_est`` per trade is the trade's net fills on the contract, not a share of the
    broker position, so the trades need not add up to it. Whatever they do not explain goes to
    one ``trade_id = None`` row; a position no fill explains is that row alone. The rows of a
    position therefore always add up to the broker's ``position_qty``.
    """
    by_pos: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
    for r in attribution:
        key = ((r.get("account_id") or "").strip(), (r.get("contract_key") or "").strip())
        if not key[0] or not key[1]:
            continue
        by_pos.setdefault(key, []).append(r)

    out: List[Dict[str, Any]] = []
    for (account_id, contract_key), group in by_pos.items():
        meta = group[0]
        position_qty = _finite(meta.get("position_qty")) or 0.0
        per_trade: Dict[Optional[int], float] = {}
        for r in group:
            tid = r.get("trade_id")
            if tid is None:
                continue
            per_trade[int(tid)] = per_trade.get(int(tid), 0.0) + (_finite(r.get("open_qty_est")) or 0.0)
        per_trade = {t: q for t, q in per_trade.items() if abs(q) > QTY_EPS}
        remainder = position_qty - sum(per_trade.values())
        shares: List[Tuple[Optional[int], float]] = sorted(per_trade.items())
        if abs(remainder) > QTY_EPS or not shares:
            shares.append((None, remainder if shares else position_qty))
        base = {
            "account_id": account_id,
            "contract_key": contract_key,
            "symbol": (meta.get("symbol") or "").strip() or None,
            "sec_type": (meta.get("sec_type") or "").strip().upper() or None,
            "expiry": parse_expiry(meta.get("expiry")),
            "strike": _finite(meta.get("strike")),
            "option_right": (meta.get("option_right") or "").strip().upper() or None,
            "position_qty": position_qty,
            "avg_cost": _finite(meta.get("avg_cost")),
        }
        # Only a live quote becomes the capture's mark. Since core 0.51.0 (TD-140) the attribution
        # reader also prices a position from the newest vendor EOD -- this table's own earlier
        # mark -- and labels it; taking that here would copy yesterday's close into today's row
        # as if it were today's, and enrich (which fills only NULL marks) would never correct it.
        mark: Optional[float] = None
        if meta.get("mark_source") == MARK_QUOTE_LIVE:
            mark = _finite(meta.get("price_last"))
            if mark is None or mark <= 0:
                mark = _finite(meta.get("price_mid"))
            if mark is not None and mark <= 0:
                mark = None
        for trade_id, qty in shares:
            out.append(
                {
                    **base,
                    "trade_id": trade_id,
                    "trade_qty": round(qty, 6),
                    "mark": mark,
                    "mark_source": MARK_QUOTE_LIVE if mark is not None else None,
                }
            )
    return out


# --------------------------------------------------------------------------- session


def session_date_ny(conn: Any) -> date:
    """Today in New York, by the database clock (the container may lack tzdata)."""
    with conn.cursor() as cur:
        cur.execute("SELECT (now() AT TIME ZONE 'America/New_York')::date")
        return cur.fetchone()[0]


def is_closed_session(conn: Any, d: date) -> bool:
    """Weekend, or a full-day NYSE holiday in ``market.us_market_holiday``; unknown -> open."""
    if d.weekday() >= 5:
        return True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM market.us_market_holiday "
                "WHERE holiday_date = %s AND upper(exchange) = 'NYSE' AND lower(status) = 'closed' LIMIT 1",
                (d,),
            )
            closed = cur.fetchone() is not None
        conn.commit()
        return closed
    except Exception as e:  # the FDW table is a convenience here, not a requirement
        conn.rollback()
        logger.warning("holiday lookup failed (%s); treating %s as a session", e, d)
        return False


def session_close_at(conn: Any, d: date) -> datetime:
    """The session's close: 16:00 New York, or the NYSE ``early-close`` time in
    ``market.us_market_holiday`` for that date. Computed by the database clock (the container may
    lack tzdata); a failed holiday lookup falls back to 16:00.
    """
    plain = "SELECT (%s::date + time '16:00') AT TIME ZONE 'America/New_York'"
    early = (
        "SELECT COALESCE("
        "(SELECT min(close_time) FROM market.us_market_holiday "
        " WHERE holiday_date = %s AND upper(exchange) = 'NYSE' AND lower(status) = 'early-close'), "
        "(%s::date + time '16:00') AT TIME ZONE 'America/New_York')"
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('market.us_market_holiday') IS NOT NULL")
            has_calendar = bool(cur.fetchone()[0])
            if has_calendar:
                cur.execute(early, (d, d))
            else:
                cur.execute(plain, (d,))
            return cur.fetchone()[0]
    except Exception as e:  # FDW unreachable: the regular close is right on all but ~3 days a year
        conn.rollback()
        logger.warning("early-close lookup failed (%s); using 16:00 New York for %s", e, d)
        with conn.cursor() as cur:
            cur.execute(plain, (d,))
            return cur.fetchone()[0]


def session_closes_at(conn: Any, dates: Iterable[date]) -> Dict[date, datetime]:
    """``session_close_at`` for many dates in one query (the snapshot reader, core 0.54.0).

    Same rule: 16:00 New York, or the NYSE ``early-close`` time in ``market.us_market_holiday``
    for that date; a calendar that is missing or unreachable falls back to 16:00.
    """
    days = sorted(set(dates))
    if not days:
        return {}
    plain = (
        "SELECT d, (d + time '16:00') AT TIME ZONE 'America/New_York' "
        "FROM unnest(%s::date[]) AS d"
    )
    early = (
        "SELECT d, COALESCE("
        "(SELECT min(close_time) FROM market.us_market_holiday "
        " WHERE holiday_date = d AND upper(exchange) = 'NYSE' AND lower(status) = 'early-close'), "
        "(d + time '16:00') AT TIME ZONE 'America/New_York') "
        "FROM unnest(%s::date[]) AS d"
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('market.us_market_holiday') IS NOT NULL")
            has_calendar = bool(cur.fetchone()[0])
            cur.execute(early if has_calendar else plain, (days,))
            return {d: close for d, close in cur.fetchall()}
    except Exception as e:  # FDW unreachable: the regular close is right on all but ~3 days a year
        conn.rollback()
        logger.warning("early-close lookup failed (%s); using 16:00 New York", e)
        with conn.cursor() as cur:
            cur.execute(plain, (days,))
            return {d: close for d, close in cur.fetchall()}


# --------------------------------------------------------------------------- capture

_SELECT_ACCOUNTS = f"""
SELECT trim(account_id) AS account_id, updated_at, net_liquidation, total_cash, buying_power,
       summary_extra
FROM {ACCOUNT}
WHERE NULLIF(trim(account_id), '') IS NOT NULL
"""

_SELECT_CAPTURED = f"SELECT account_id FROM {ACCOUNT_NAV_DAILY} WHERE snapshot_date = %s"

_INSERT_NAV = f"""
INSERT INTO {ACCOUNT_NAV_DAILY}
    (snapshot_date, account_id, net_liquidation, total_cash, buying_power, cushion,
     excess_liquidity, maint_margin_req, account_updated_at)
VALUES (%(snapshot_date)s, %(account_id)s, %(net_liquidation)s, %(total_cash)s, %(buying_power)s,
        %(cushion)s, %(excess_liquidity)s, %(maint_margin_req)s, %(updated_at)s)
ON CONFLICT (snapshot_date, account_id) DO NOTHING
"""

_INSERT_POSITION = f"""
INSERT INTO {POSITION_SNAPSHOT_DAILY}
    (snapshot_date, account_id, contract_key, trade_id, symbol, sec_type, expiry, strike,
     option_right, position_qty, trade_qty, avg_cost, mark, mark_source, positions_updated_at)
VALUES (%(snapshot_date)s, %(account_id)s, %(contract_key)s, %(trade_id)s, %(symbol)s,
        %(sec_type)s, %(expiry)s, %(strike)s, %(option_right)s, %(position_qty)s,
        %(trade_qty)s, %(avg_cost)s, %(mark)s, %(mark_source)s, %(positions_updated_at)s)
ON CONFLICT ON CONSTRAINT position_snapshot_daily_uq DO NOTHING
"""

#: ``account_nav_daily`` column -> IB account-summary tag in ``brokerage.account.summary_extra``.
SUMMARY_EXTRA_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("cushion", "Cushion"),
    ("excess_liquidity", "ExcessLiquidity"),
    ("maint_margin_req", "MaintMarginReq"),
)


def summary_extra_values(extra: Any) -> Dict[str, Optional[float]]:
    """The three margin values from ``summary_extra`` (IB sends strings); missing or non-finite -> None."""
    src = extra if isinstance(extra, Mapping) else {}
    return {col: _finite(src.get(tag)) for col, tag in SUMMARY_EXTRA_COLUMNS}


def _iso(ts: Any) -> Optional[str]:
    return ts.isoformat() if hasattr(ts, "isoformat") else (str(ts) if ts is not None else None)


def _positions_meta(conn: Any) -> Dict[Tuple[str, str], Any]:
    """(account_id, contract_key) -> positions.updated_at, for the open positions."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT trim(account_id), trim(contract_key), updated_at FROM {POSITIONS} WHERE position != 0"
        )
        return {(a, k): u for a, k, u in cur.fetchall()}


def attribution_live_marks_only(conn: Any) -> List[Dict[str, Any]]:
    """The attribution read capture uses: no vendor-EOD fallback (TD-140).

    The snapshot's mark is the live quote at capture or nothing; enrich fills the session's own
    close. ``split_rows`` refuses a non-live mark as well, so a caller passing its own reader
    cannot feed the table its previous mark either.
    """
    from bifrost_core.portfolio.reader.executions import get_position_instance_attribution

    return get_position_instance_attribution(conn, fallback_marks=False)


def _accounts(conn: Any) -> List[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(_SELECT_ACCOUNTS)
        return [dict(r) for r in cur.fetchall()]


def _captured_accounts(conn: Any, snapshot_date: date) -> set:
    with conn.cursor() as cur:
        cur.execute(_SELECT_CAPTURED, (snapshot_date,))
        return {a for (a,) in cur.fetchall()}


def capture(
    conn: Any,
    snapshot_date: date,
    *,
    attribution: Optional[Callable[[Any], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Write the day's NAV and positions for each account that is fresh and not yet captured.

    Fresh: ``brokerage.account.updated_at`` at or after the session's close (``session_close_at``).
    The account row is the freshness signal for the account's positions too: the broker sync writes
    an account's positions with its account row, while a position row's own ``updated_at`` only
    moves when that position changes. A stale account (or open positions whose account has no
    account row) is skipped whole and listed in ``stale_accounts``; that is not a failure. An
    account with a NAV row for the date already is left as written (``already_captured``), so the
    first capture of an account is the one kept and a rerun never mixes two reads of its book.

    Raises SnapshotError when an account to be written has open positions but the attribution read
    returned none for it (that reader logs and answers [] on failure, which must not pass for an
    empty book). Nothing is written then. When no account is to be written the attribution is not
    read at all.
    """
    if attribution is None:
        attribution = attribution_live_marks_only

    close_at = session_close_at(conn, snapshot_date)
    accounts = _accounts(conn)
    captured = _captured_accounts(conn, snapshot_date)
    open_positions = _positions_meta(conn)

    pending: Dict[str, Dict[str, Any]] = {}
    stale: Dict[str, Any] = {}
    for a in accounts:
        acct = a["account_id"]
        if acct in captured:
            continue
        updated_at = a.get("updated_at")
        if updated_at is None or updated_at < close_at:
            stale[acct] = updated_at
        else:
            pending[acct] = a
    for acct, _ in open_positions:
        if acct not in pending and acct not in captured and acct not in stale:
            stale[acct] = None  # positions with no account row: no evidence they are current

    result: Dict[str, Any] = {
        "nav_rows": 0,
        "position_rows": 0,
        "position_rows_seen": 0,
        "session_close": _iso(close_at),
        "stale_accounts": [{"account_id": k, "updated_at": _iso(v)} for k, v in sorted(stale.items())],
        "already_captured": sorted(captured),
    }
    if not pending:
        conn.rollback()
        return result

    pending_positions = [k for k in open_positions if k[0] in pending]
    attr = [r for r in (attribution(conn) or []) if (r.get("account_id") or "").strip() in pending]
    try:
        conn.rollback()  # the reader leaves its read transaction open
    except Exception:
        pass
    if pending_positions and not attr:
        raise SnapshotError(
            f"{len(pending_positions)} open positions in {sorted(pending)} but the attribution read "
            "returned none; nothing written"
        )
    rows = split_rows(attr)
    with conn.cursor() as cur:
        nav = 0
        for acct, a in sorted(pending.items()):
            cur.execute(
                _INSERT_NAV,
                {
                    "snapshot_date": snapshot_date,
                    "account_id": acct,
                    "net_liquidation": _finite(a.get("net_liquidation")),
                    "total_cash": _finite(a.get("total_cash")),
                    "buying_power": _finite(a.get("buying_power")),
                    "updated_at": a.get("updated_at"),
                    **summary_extra_values(a.get("summary_extra")),
                },
            )
            nav += cur.rowcount
        written = 0
        for r in rows:
            cur.execute(
                _INSERT_POSITION,
                {
                    **r,
                    "snapshot_date": snapshot_date,
                    "positions_updated_at": open_positions.get((r["account_id"], r["contract_key"])),
                },
            )
            written += cur.rowcount
    conn.commit()
    result.update({"nav_rows": nav, "position_rows": written, "position_rows_seen": len(rows)})
    return result


# --------------------------------------------------------------------------- enrich


def _default_option_rows(symbol: str, expiry: date, as_of: date) -> List[Dict[str, Any]]:
    from bifrost_core.monitor.market_read_client import _get_json

    resp = _get_json(
        "/options/snapshots",
        {"symbol": symbol, "expiration": expiry.isoformat(), "as_of": as_of.isoformat(), "limit": "5000"},
    )
    return list(resp.get("rows") or [])


def _default_closes(symbols: List[str], as_of: date) -> Dict[str, Dict[str, Any]]:
    from bifrost_core.monitor.market_read_client import get_bars_benchmark_via_plugin

    return get_bars_benchmark_via_plugin(symbols, on_or_before=as_of.isoformat())


def _close_on(bar: Optional[Mapping[str, Any]], d: date) -> Optional[float]:
    """The bar's close when the bar is that session's (a bar from an earlier day is not).

    The plugin's ``/stocks/db/bars/benchmark`` sends ``bar_time`` as epoch seconds of the bar
    date (UTC midnight) and ``close`` 0 when it has none; an ISO date string is accepted too.
    """
    if not bar:
        return None
    raw = bar.get("bar_time")
    epoch = _finite(raw)
    if epoch is not None:
        if epoch <= 0:
            return None
        bar_day = datetime.fromtimestamp(epoch, tz=timezone.utc).date()
    else:
        bar_day = parse_expiry(raw)
    if bar_day != d:
        return None
    close = _finite(bar.get("close"))
    return close if close is not None and close > 0 else None


_SELECT_TO_ENRICH = f"""
SELECT position_snapshot_daily_id, symbol, sec_type, expiry, strike, option_right,
       mark, underlying_close, delta, iv
FROM {POSITION_SNAPSHOT_DAILY}
WHERE snapshot_date = %s
  AND (underlying_close IS NULL OR mark IS NULL
       OR (upper(coalesce(sec_type, '')) = 'OPT' AND (delta IS NULL OR iv IS NULL)))
"""

_UPDATE_ENRICH = f"""
UPDATE {POSITION_SNAPSHOT_DAILY} SET
    underlying_close = COALESCE(underlying_close, %(underlying_close)s),
    delta = COALESCE(delta, %(delta)s),
    gamma = COALESCE(gamma, %(gamma)s),
    vega = COALESCE(vega, %(vega)s),
    theta = COALESCE(theta, %(theta)s),
    iv = COALESCE(iv, %(iv)s),
    greeks_asof = COALESCE(greeks_asof, %(greeks_asof)s),
    mark_source = CASE WHEN mark IS NULL AND %(mark)s IS NOT NULL THEN %(mark_source)s ELSE mark_source END,
    mark = COALESCE(mark, %(mark)s)
WHERE position_snapshot_daily_id = %(id)s
"""


def enrich(
    conn: Any,
    snapshot_date: date,
    *,
    option_rows: Callable[[str, date, date], List[Dict[str, Any]]] = _default_option_rows,
    closes: Callable[[List[str], date], Dict[str, Dict[str, Any]]] = _default_closes,
) -> Dict[str, int]:
    """Fill the vendor EOD values the plugin has for the session; only NULLs change."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(_SELECT_TO_ENRICH, (snapshot_date,))
        rows = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    if not rows:
        return {"rows": 0, "updated": 0, "greeks_missing": 0}

    symbols = sorted({(r["symbol"] or "").strip().upper() for r in rows if r.get("symbol")})
    try:
        bars = closes(symbols, snapshot_date) or {}
    except Exception as e:
        logger.warning("underlying closes unavailable: %s", e)
        bars = {}
    close_by_symbol = {s.upper(): _close_on(b, snapshot_date) for s, b in bars.items()}

    chains: Dict[Tuple[str, date], Dict[str, Dict[str, Any]]] = {}
    updated = 0
    greeks_missing = 0
    with conn.cursor() as cur:
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            under_close = close_by_symbol.get(sym)
            vals: Dict[str, Any] = {
                "id": r["position_snapshot_daily_id"],
                "underlying_close": under_close,
                "delta": None,
                "gamma": None,
                "vega": None,
                "theta": None,
                "iv": None,
                "greeks_asof": None,
                "mark": None,
                "mark_source": MARK_VENDOR_EOD,
            }
            if (r.get("sec_type") or "").upper() == "OPT":
                exp = r.get("expiry")
                ticker = vendor_option_ticker(sym, exp, r.get("strike"), r.get("option_right"))
                hit: Optional[Mapping[str, Any]] = None
                if ticker and exp is not None:
                    key = (sym, exp)
                    if key not in chains:
                        try:
                            chains[key] = {
                                str(x.get("option_ticker")): x for x in option_rows(sym, exp, snapshot_date)
                            }
                        except Exception as e:
                            logger.warning("option snapshots %s %s unavailable: %s", sym, exp, e)
                            chains[key] = {}
                    hit = chains[key].get(ticker)
                if hit:
                    for g in ("delta", "gamma", "vega", "theta", "iv"):
                        vals[g] = _finite(hit.get(g))
                    vals["greeks_asof"] = hit.get("snapshot_ts")
                    vals["mark"] = _finite(hit.get("day_close"))
                elif r.get("delta") is None:
                    greeks_missing += 1
            else:
                vals["mark"] = under_close
            if all(vals[k] is None for k in ("underlying_close", "delta", "iv", "mark")):
                continue
            cur.execute(_UPDATE_ENRICH, vals)
            updated += cur.rowcount
    conn.commit()
    return {"rows": len(rows), "updated": updated, "greeks_missing": greeks_missing}
