"""TD-246 / TD-250 ratchet: enrich never stores a stale option close as the vendor's close.

The vendor's ``day_close`` is the contract's last trade; a thin contract's can be days old and sit
under the option's intrinsic value at the underlying's close (TD-246), or be an earlier session's
trade or a morning trade far from the vendor-IV price (TD-250, the plugin's ``last_trade_ts``).
Enrich replaces such a close and labels the replacement with its own ``mark_source``; the reader
grades every label on purpose. All numbers here are made up.
"""

from __future__ import annotations

import ast
import itertools
import pathlib
from datetime import date, datetime, timedelta, timezone

import pytest

from bifrost_core.portfolio.quote_freshness import (
    MARK_EOD_SOURCES,
    MARK_INTRINSIC_FLOOR,
    MARK_SOURCES,
    MARK_VENDOR_EOD,
    MARK_VENDOR_IV_MODEL,
)
from bifrost_core.portfolio.reader import snapshots as reader
from bifrost_core.portfolio.snapshot import daily

SESSION = date(2026, 10, 5)
#: New York midnight and 16:00 of SESSION (EDT), as ``session_bounds_at`` reads them from the DB.
BOUNDS = (datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc), datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc))
#: The plugin before 0.85.0: the row has no ``last_trade_ts`` key at all.
ABSENT = object()
SRC = pathlib.Path(daily.__file__).resolve().parents[2]  # src/bifrost_core


class _Cur:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if sql.lstrip().startswith("SELECT"):
            self._rows = self.store["select"]
        else:
            self.store["updates"].append(params)
            self.rowcount = 1

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self.store = {"select": rows, "updates": []}

    def cursor(self, **kw):
        return _Cur(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass


def _enrich_one(*, strike, right, expiry, day_close, iv, underlying, stored_underlying=None,
                last_trade_ts=ABSENT, bounds=lambda conn, d: BOUNDS):
    ticker = daily.vendor_option_ticker("ZZZ", expiry, strike, right)
    conn = _Conn(
        [{"position_snapshot_daily_id": 1, "symbol": "ZZZ", "sec_type": "OPT", "expiry": expiry,
          "strike": strike, "option_right": right, "mark": None, "underlying_close": stored_underlying,
          "delta": None, "iv": None}]
    )
    hit = {"option_ticker": ticker, "delta": 0.5, "gamma": 0.01, "vega": 0.2, "theta": -0.05,
           "iv": iv, "day_close": day_close, "snapshot_ts": "2026-10-05T20:00:00Z"}
    if last_trade_ts is not ABSENT:
        hit["last_trade_ts"] = last_trade_ts
    daily.enrich(conn, SESSION, option_rows=lambda s, e, a: [hit],
                 closes=lambda syms, as_of: {"ZZZ": {"bar_time": 1791158400.0, "close": underlying}},
                 session_bounds=bounds)
    (u,) = conn.store["updates"]
    return u


#: last_trade_ts values the plugin may send, by what enrich must make of them.
STAMP_AFTER_CLOSE = "2026-10-05T20:15:03.216000+00:00"  # traded at the close (stamp ~15 min late)
STAMP_INSIDE_WINDOW = (BOUNDS[1] - timedelta(minutes=daily.STALE_TRADE_MINUTES - 1)).isoformat()
STAMP_MORNING = "2026-10-05T13:48:03.112Z"  # the same session, hours before the close
STAMP_EARLIER_SESSION = "2026-09-24T14:06:02.479+00:00"
NOT_JUDGED = (ABSENT, None, "not-a-time", STAMP_AFTER_CLOSE, STAMP_INSIDE_WINDOW)


# --------------------------------------------------------------------------- the writer


def test_a_stale_close_under_intrinsic_takes_the_vendor_iv_price():
    # A deep in-the-money LEAP call: the last trade (70) is far under intrinsic (370 - 280 = 90).
    u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=70.0, iv=0.65, underlying=370.0)
    assert u["mark_source"] == MARK_VENDOR_IV_MODEL
    assert u["mark"] > 90.0  # intrinsic plus the time value the vendor's IV gives it
    assert u["mark"] == pytest.approx(
        daily.bs_price(370.0, 280.0, 102 / 365.0, daily.RATE_POSITIONS_MODEL, 0.65, "C")
    )


def test_no_iv_or_expiry_day_falls_to_the_intrinsic_floor():
    u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=70.0, iv=None, underlying=370.0)
    assert (u["mark"], u["mark_source"]) == (pytest.approx(90.0), MARK_INTRINSIC_FLOOR)
    u = _enrich_one(strike=280.0, right="C", expiry=SESSION, day_close=70.0, iv=0.65, underlying=370.0)
    assert (u["mark"], u["mark_source"]) == (pytest.approx(90.0), MARK_INTRINSIC_FLOOR)


def test_a_deep_put_whose_european_price_is_under_intrinsic_takes_the_floor():
    # Two years out, r > 0, vol low: the European put is worth less than K - S.
    u = _enrich_one(strike=200.0, right="P", expiry=date(2028, 10, 20), day_close=50.0, iv=0.05, underlying=120.0)
    assert (u["mark"], u["mark_source"]) == (pytest.approx(80.0), MARK_INTRINSIC_FLOOR)


def test_a_close_at_or_over_intrinsic_is_kept_as_the_vendor_close():
    u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=104.0, iv=0.65, underlying=370.0)
    assert (u["mark"], u["mark_source"]) == (104.0, MARK_VENDOR_EOD)
    u = _enrich_one(strike=40.0, right="P", expiry=date(2026, 11, 20), day_close=1.25, iv=0.4, underlying=52.0)
    assert (u["mark"], u["mark_source"]) == (1.25, MARK_VENDOR_EOD)  # out of the money: intrinsic 0


def test_the_stored_underlying_close_is_the_one_judged_against():
    # The row already holds 300 (the UPDATE keeps it): intrinsic 20, so a close of 70 stands.
    u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=70.0, iv=0.65,
                    underlying=370.0, stored_underlying=300.0)
    assert (u["mark"], u["mark_source"]) == (70.0, MARK_VENDOR_EOD)


def test_without_an_underlying_close_the_close_cannot_be_judged_and_is_kept():
    assert daily.option_eod_mark(70.0, iv=0.65, strike=280.0, right="C", expiry=date(2027, 1, 15),
                                 underlying_close=None, session=SESSION) == (70.0, MARK_VENDOR_EOD)
    assert daily.option_eod_mark(None, iv=0.65, strike=280.0, right="C", expiry=date(2027, 1, 15),
                                 underlying_close=370.0, session=SESSION) == (None, MARK_VENDOR_EOD)


@pytest.mark.parametrize(
    "right, strike, underlying, day_close, iv, expiry, last_trade_ts",
    list(itertools.product(
        ("C", "P"),
        (50.0, 100.0, 150.0),
        (40.0, 100.0, 160.0),
        (0.01, 2.0, 30.0, 80.0),
        (None, 0.0, 0.05, 0.4, 1.5, float("nan")),
        (SESSION, date(2026, 10, 9), date(2027, 1, 15), date(2028, 12, 15)),
        (*NOT_JUDGED, STAMP_MORNING, STAMP_EARLIER_SESSION),
    )),
)
def test_enrich_never_stores_a_stale_close_as_the_vendor_close(right, strike, underlying, day_close, iv, expiry,
                                                               last_trade_ts):
    """The ratchet: whatever the vendor sends, the stored mark is not under intrinsic, a mark that
    is not the vendor's close never carries the vendor's label, a close is replaced only when it is
    under intrinsic or its trade time says it is stale, and without a usable trade time (an older
    plugin, a never-traded contract, a trade at the close) enrich does what it did before."""
    u = _enrich_one(strike=strike, right=right, expiry=expiry, day_close=day_close, iv=iv, underlying=underlying,
                    last_trade_ts=last_trade_ts)
    base = daily.option_eod_mark(day_close, iv=iv, strike=strike, right=right, expiry=expiry,
                                 underlying_close=underlying, session=SESSION)
    intrinsic = daily.intrinsic_value(strike, right, underlying)
    under = day_close < intrinsic - daily.INTRINSIC_TOLERANCE
    model = daily._vendor_iv_price(iv, strike, right, expiry, underlying, SESSION)
    assert u["mark_source"] in MARK_EOD_SOURCES
    assert u["mark"] >= intrinsic - daily.INTRINSIC_TOLERANCE
    if u["mark_source"] == MARK_VENDOR_EOD:
        assert u["mark"] == day_close
    else:
        assert under or last_trade_ts in (STAMP_MORNING, STAMP_EARLIER_SESSION)
    if last_trade_ts in NOT_JUDGED:
        assert (u["mark"], u["mark_source"]) == base  # the TD-246 rule alone
    elif not under and model is not None:
        reference = max(model, intrinsic)
        far = abs(day_close - reference) > max(daily.STALE_TRADE_DEVIATION * reference, daily.STALE_TRADE_DEVIATION_MIN)
        # An earlier session's trade is never the session's close; a morning one is when it is near.
        assert (u["mark_source"] == MARK_VENDOR_EOD) == (last_trade_ts == STAMP_MORNING and not far)
    else:
        assert (u["mark"], u["mark_source"]) == base  # no model to judge by, or already replaced
    row = {"sec_type": "OPT", "strike": strike, "option_right": right, "underlying_close": underlying, "mark": u["mark"]}
    assert reader.mark_below_intrinsic(row) is False  # the reader's flag agrees with the writer


# --------------------------------------------------------------------------- TD-250 by example


def test_a_morning_trade_far_over_the_vendor_iv_price_takes_that_price():
    # Intrinsic 90; the vendor-IV price is about 104; the morning trade 140 is a third over it.
    u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=140.0, iv=0.65, underlying=370.0,
                    last_trade_ts=STAMP_MORNING)
    assert u["mark_source"] == MARK_VENDOR_IV_MODEL
    assert u["mark"] == pytest.approx(
        daily.bs_price(370.0, 280.0, 102 / 365.0, daily.RATE_POSITIONS_MODEL, 0.65, "C")
    )


def test_a_morning_trade_near_the_vendor_iv_price_is_kept():
    model = daily.bs_price(370.0, 280.0, 102 / 365.0, daily.RATE_POSITIONS_MODEL, 0.65, "C")
    u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=round(model * 1.1, 2), iv=0.65,
                    underlying=370.0, last_trade_ts=STAMP_MORNING)
    assert (u["mark"], u["mark_source"]) == (round(model * 1.1, 2), MARK_VENDOR_EOD)


def test_the_same_far_close_traded_inside_the_window_or_at_the_close_is_kept():
    for stamp in (STAMP_INSIDE_WINDOW, STAMP_AFTER_CLOSE):
        u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=140.0, iv=0.65,
                        underlying=370.0, last_trade_ts=stamp)
        assert (u["mark"], u["mark_source"]) == (140.0, MARK_VENDOR_EOD), stamp


def test_an_earlier_sessions_trade_is_replaced_even_near_the_price():
    model = daily.bs_price(370.0, 280.0, 102 / 365.0, daily.RATE_POSITIONS_MODEL, 0.65, "C")
    u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=round(model * 1.02, 2), iv=0.65,
                    underlying=370.0, last_trade_ts=STAMP_EARLIER_SESSION)
    assert (u["mark"], u["mark_source"]) == (pytest.approx(model), MARK_VENDOR_IV_MODEL)


def test_an_earlier_sessions_trade_with_no_iv_is_kept_as_before():
    u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=104.0, iv=None, underlying=370.0,
                    last_trade_ts=STAMP_EARLIER_SESSION)
    assert (u["mark"], u["mark_source"]) == (104.0, MARK_VENDOR_EOD)


def test_a_stale_deep_put_over_intrinsic_whose_model_is_under_it_takes_the_floor():
    # Two years out, low vol: the European put is under K - S = 80; the old trade 95 is stale.
    u = _enrich_one(strike=200.0, right="P", expiry=date(2028, 10, 20), day_close=95.0, iv=0.05, underlying=120.0,
                    last_trade_ts=STAMP_EARLIER_SESSION)
    assert (u["mark"], u["mark_source"]) == (pytest.approx(80.0), MARK_INTRINSIC_FLOOR)


def test_without_the_sessions_bounds_the_trade_time_is_not_judged():
    def unavailable(conn, d):
        raise RuntimeError("FDW down")

    u = _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=140.0, iv=0.65, underlying=370.0,
                    last_trade_ts=STAMP_EARLIER_SESSION, bounds=unavailable)
    assert (u["mark"], u["mark_source"]) == (140.0, MARK_VENDOR_EOD)


def test_the_bounds_are_read_once_and_before_any_update():
    calls = []

    def bounds(conn, d):
        calls.append(list(conn.store["updates"]))
        return BOUNDS

    _enrich_one(strike=280.0, right="C", expiry=date(2027, 1, 15), day_close=140.0, iv=0.65, underlying=370.0,
                last_trade_ts=STAMP_MORNING, bounds=bounds)
    assert calls == [[]]


@pytest.mark.parametrize("stamp, stale", [
    (datetime(2026, 10, 5, 13, 48, 3, tzinfo=timezone.utc), True),
    (datetime(2026, 10, 5, 13, 48, 3), True),  # naive = UTC
    ("2026-10-05T09:48:03-04:00", True),
    ("2026-10-04T23:59:59-04:00", True),  # the night before: an earlier session even when near
    ("2026-10-05T19:31:00+00:00", False),  # 29 minutes before the close
    ("2026-10-05T20:15:03+00:00", False),
    ("", False),
    (1791216483, False),  # epoch numbers are not a format the plugin sends
])
def test_stale_close_reads_the_trade_time_the_plugin_sends(stamp, stale):
    # Close 140 against a reference of 104: far, so the time alone decides.
    assert daily.stale_close(140.0, 104.0, stamp, BOUNDS) is stale


def test_enrich_takes_the_option_mark_only_through_option_eod_mark():
    """No path in core stores the vendor's ``day_close`` except ``option_eod_mark``'s argument."""
    hits = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and node.value == "day_close":
                hits.append(path.relative_to(SRC).as_posix())
    assert hits == ["portfolio/snapshot/daily.py"]
    source = (SRC / "portfolio/snapshot/daily.py").read_text(encoding="utf-8")
    assert source.count('"day_close"') == 1 and 'option_eod_mark(\n                        hit.get("day_close")' in source


# --------------------------------------------------------------------------- the vocabulary


def test_every_mark_source_has_a_greeks_grade():
    """A new mark_source is graded on purpose: it lands in exactly one of the reader's two sets."""
    graded = reader.MARKS_WITH_THE_GREEKS | reader.MARKS_WITHOUT_THE_GREEKS
    assert set(MARK_SOURCES) == graded
    assert not reader.MARKS_WITH_THE_GREEKS & reader.MARKS_WITHOUT_THE_GREEKS
    assert set(MARK_EOD_SOURCES) <= set(MARK_SOURCES)


def _vendor_row(**kw):
    row = {"sec_type": "OPT", "delta": 0.8, "gamma": 0.01, "vega": 0.3, "theta": -0.05, "iv": 0.6,
           "greeks_session": SESSION, "mark_source": MARK_VENDOR_EOD}
    row.update(kw)
    return row


def test_the_vendor_iv_price_keeps_the_vendor_grade_and_the_floor_degrades():
    assert reader.greeks_quality(_vendor_row(mark_source=MARK_VENDOR_IV_MODEL), SESSION) == ("vendor", None)
    q, why = reader.greeks_quality(_vendor_row(mark_source=MARK_INTRINSIC_FLOOR), SESSION)
    assert q == "degraded" and "intrinsic_floor" in why
