"""The nightly book snapshot (W4, core 0.48.0): positions per trade and account NAV.

Two steps, both idempotent for a session date:

* ``capture`` -- right after the close. Reads the broker's current book (``brokerage.positions``
  and ``brokerage.account``, FDW to Golden Source) and the trade attribution of each position,
  and writes ``position_snapshot_daily`` / ``account_nav_daily``. This is the part that cannot be
  recovered later: the broker tables hold the current state only. A row already written for the
  date is kept (``ON CONFLICT DO NOTHING``), so a second run never rewrites what the first saw.
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
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.brokerage_tables import ACCOUNT, POSITIONS
from bifrost_core.persistence.postgres.snapshot_ddl import ACCOUNT_NAV_DAILY, POSITION_SNAPSHOT_DAILY

logger = logging.getLogger(__name__)

#: Below this a split remainder is float noise, not an unattributed position.
QTY_EPS = 1e-6

MARK_QUOTE_LIVE = "quote_live"
MARK_VENDOR_EOD = "vendor_eod"


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


# --------------------------------------------------------------------------- capture

_INSERT_NAV = f"""
INSERT INTO {ACCOUNT_NAV_DAILY}
    (snapshot_date, account_id, net_liquidation, total_cash, buying_power, account_updated_at)
SELECT %s, account_id, net_liquidation, total_cash, buying_power, updated_at
FROM {ACCOUNT}
WHERE NULLIF(trim(account_id), '') IS NOT NULL
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


def _positions_meta(conn: Any) -> Dict[Tuple[str, str], Any]:
    """(account_id, contract_key) -> positions.updated_at, for the open positions."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT trim(account_id), trim(contract_key), updated_at FROM {POSITIONS} WHERE position != 0"
        )
        return {(a, k): u for a, k, u in cur.fetchall()}


def capture(
    conn: Any,
    snapshot_date: date,
    *,
    attribution: Optional[Callable[[Any], List[Dict[str, Any]]]] = None,
) -> Dict[str, int]:
    """Write the day's NAV and positions; rows already there for the date are kept.

    Raises SnapshotError when the broker has open positions but the attribution read returned
    none (that reader logs and answers [] on failure, which must not pass for an empty book).
    """
    if attribution is None:
        from bifrost_core.portfolio.reader.executions import get_position_instance_attribution

        attribution = get_position_instance_attribution

    open_positions = _positions_meta(conn)
    attr = attribution(conn) or []
    try:
        conn.rollback()  # the reader leaves its read transaction open
    except Exception:
        pass
    if open_positions and not attr:
        raise SnapshotError(
            f"{len(open_positions)} open positions but the attribution read returned none; nothing written"
        )
    rows = split_rows(attr)
    with conn.cursor() as cur:
        cur.execute(_INSERT_NAV, (snapshot_date,))
        nav = cur.rowcount
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
    return {"nav_rows": nav, "position_rows": written, "position_rows_seen": len(rows)}


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
    """The bar's close when the bar is that session's (a bar from an earlier day is not)."""
    if not bar:
        return None
    if str(bar.get("bar_time") or "")[:10] != d.isoformat():
        return None
    return _finite(bar.get("close"))


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
