"""TD-246 ratchet: enrich never stores an option mark under intrinsic as the vendor's close.

The vendor's ``day_close`` is the contract's last trade; a thin contract's can be days old and sit
under the option's intrinsic value at the underlying's close. Enrich replaces such a close and
labels the replacement with its own ``mark_source``; the reader grades every label on purpose.
All numbers here are made up.
"""

from __future__ import annotations

import ast
import itertools
import pathlib
from datetime import date

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


def _enrich_one(*, strike, right, expiry, day_close, iv, underlying, stored_underlying=None):
    ticker = daily.vendor_option_ticker("ZZZ", expiry, strike, right)
    conn = _Conn(
        [{"position_snapshot_daily_id": 1, "symbol": "ZZZ", "sec_type": "OPT", "expiry": expiry,
          "strike": strike, "option_right": right, "mark": None, "underlying_close": stored_underlying,
          "delta": None, "iv": None}]
    )
    chain = [{"option_ticker": ticker, "delta": 0.5, "gamma": 0.01, "vega": 0.2, "theta": -0.05,
              "iv": iv, "day_close": day_close, "snapshot_ts": "2026-10-05T20:00:00Z"}]
    daily.enrich(conn, SESSION, option_rows=lambda s, e, a: chain,
                 closes=lambda syms, as_of: {"ZZZ": {"bar_time": 1791158400.0, "close": underlying}})
    (u,) = conn.store["updates"]
    return u


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
    "right, strike, underlying, day_close, iv, expiry",
    list(itertools.product(
        ("C", "P"),
        (50.0, 100.0, 150.0),
        (40.0, 100.0, 160.0),
        (0.01, 2.0, 30.0, 80.0),
        (None, 0.0, 0.05, 0.4, 1.5, float("nan")),
        (SESSION, date(2026, 10, 9), date(2027, 1, 15), date(2028, 12, 15)),
    )),
)
def test_enrich_never_stores_a_mark_under_intrinsic_as_the_vendor_close(right, strike, underlying, day_close, iv, expiry):
    """The ratchet: whatever the vendor sends, the stored mark is not under intrinsic, and a mark
    that is not the vendor's close never carries the vendor's label."""
    u = _enrich_one(strike=strike, right=right, expiry=expiry, day_close=day_close, iv=iv, underlying=underlying)
    intrinsic = daily.intrinsic_value(strike, right, underlying)
    assert u["mark_source"] in MARK_EOD_SOURCES
    assert u["mark"] >= intrinsic - daily.INTRINSIC_TOLERANCE
    if u["mark_source"] == MARK_VENDOR_EOD:
        assert u["mark"] == day_close
    else:
        assert day_close < intrinsic - daily.INTRINSIC_TOLERANCE
    row = {"sec_type": "OPT", "strike": strike, "option_right": right, "underlying_close": underlying, "mark": u["mark"]}
    assert reader.mark_below_intrinsic(row) is False  # the reader's flag agrees with the writer


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
