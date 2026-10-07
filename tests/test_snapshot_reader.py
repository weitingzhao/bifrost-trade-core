"""The snapshot reader's pure parts (TD-138 / TD-139, core 0.54.0): greeks_quality, the one-session
attribution of a row, the prior session. Values are invented."""

from __future__ import annotations

from datetime import date

import pytest

from bifrost_core.portfolio.reader import snapshots as s

D0 = date(2026, 10, 5)
D1 = date(2026, 10, 6)


def _opt(**kw):
    row = {
        "sec_type": "OPT",
        "trade_qty": -2.0,
        "mark": 1.50,
        "mark_source": "vendor_eod",
        "underlying_close": 50.0,
        "delta": -0.30,
        "gamma": 0.04,
        "vega": 0.08,
        "theta": -0.02,
        "iv": 0.40,
        "greeks_session": D0,
    }
    row.update(kw)
    return row


# --------------------------------------------------------------------------- TD-139


def test_greeks_quality_vendor_when_the_session_s_vendor_values_are_all_there():
    assert s.greeks_quality(_opt(), D0) == ("vendor", None)


def test_greeks_quality_missing_when_there_is_no_delta():
    q, why = s.greeks_quality(_opt(delta=None), D0)
    assert q == "missing" and "no Greeks" in why


@pytest.mark.parametrize(
    "kw, fragment",
    [
        ({"greeks_session": date(2026, 10, 2)}, "as of 2026-10-02"),
        ({"greeks_session": None}, "no as-of time"),
        ({"mark_source": "quote_live"}, "quote_live"),
        ({"mark_source": None}, "no mark"),
        ({"vega": None}, "vega missing"),
        ({"iv": float("nan")}, "iv missing"),
    ],
)
def test_greeks_quality_degraded_cases(kw, fragment):
    q, why = s.greeks_quality(_opt(**kw), D0)
    assert q == "degraded"
    assert fragment in why


def test_a_stock_row_has_no_greeks_quality():
    assert s.greeks_quality({"sec_type": "STK", "delta": None}, D0) == (None, None)


# --------------------------------------------------------------------------- attribution


def test_option_row_parts_and_the_identity():
    prior = _opt(greeks_quality="vendor")
    current = _opt(mark=1.20, underlying_close=51.0, iv=0.38, greeks_session=D1)
    r = s.attribute_row(prior, current, days=1)
    q = -2.0 * 100.0
    assert r["status"] == "ok" and r["greeks_quality"] == "vendor"
    assert r["held_pnl"] == pytest.approx(q * (1.20 - 1.50))
    assert r["delta_pnl"] == pytest.approx(q * -0.30 * 1.0)
    assert r["gamma_pnl"] == pytest.approx(q * 0.5 * 0.04 * 1.0)
    # vega is per vol point: iv 0.40 -> 0.38 is -2 points
    assert r["vega_pnl"] == pytest.approx(q * 0.08 * -2.0)
    assert r["theta_pnl"] == pytest.approx(q * -0.02 * 1)
    parts = r["delta_pnl"] + r["gamma_pnl"] + r["vega_pnl"] + r["theta_pnl"]
    assert r["unexplained"] == pytest.approx(r["held_pnl"] - parts)


def test_theta_counts_calendar_days_across_a_weekend():
    r = s.attribute_row(_opt(greeks_quality="vendor"), _opt(), days=3)
    assert r["theta_pnl"] == pytest.approx(-2.0 * 100.0 * -0.02 * 3)


def test_missing_greeks_leave_the_parts_and_the_residual_unread_but_keep_the_held_pnl():
    prior = _opt(delta=None, gamma=None, vega=None, theta=None, iv=None, greeks_quality="missing")
    r = s.attribute_row(prior, _opt(mark=1.0), days=1)
    assert r["status"] == "ok" and r["greeks_quality"] == "missing"
    assert r["held_pnl"] == pytest.approx(-200.0 * (1.0 - 1.5))
    assert r["delta_pnl"] is None and r["unexplained"] is None


def test_stock_row_is_all_delta():
    prior = {"sec_type": "STK", "trade_qty": 10.0, "mark": 20.0, "underlying_close": 20.0}
    current = {"sec_type": "STK", "trade_qty": 10.0, "mark": 21.0, "underlying_close": 21.0}
    r = s.attribute_row(prior, current, days=1)
    assert (r["held_pnl"], r["delta_pnl"], r["gamma_pnl"], r["vega_pnl"], r["theta_pnl"]) == (10.0, 10.0, 0.0, 0.0, 0.0)
    assert r["unexplained"] == 0.0 and r["greeks_quality"] is None


@pytest.mark.parametrize(
    "prior, current, status",
    [
        (None, _opt(), "opened_in_session"),
        (_opt(), None, "closed_in_session"),
        (_opt(), _opt(mark=None), "no_mark"),
        (_opt(), _opt(underlying_close=None), "no_mark"),
    ],
)
def test_rows_that_cannot_be_differenced_read_nothing(prior, current, status):
    r = s.attribute_row(prior, current, days=1)
    assert r["status"] == status
    assert r["unexplained"] is None and r["delta_pnl"] is None


def test_previous_session_skips_weekends_and_holidays():
    assert s.previous_session(date(2026, 10, 6), set()) == date(2026, 10, 5)
    assert s.previous_session(date(2026, 10, 5), set()) == date(2026, 10, 2)  # Monday -> Friday
    assert s.previous_session(date(2026, 11, 27), {date(2026, 11, 26)}) == date(2026, 11, 25)


def test_trade_rollup_keeps_unattributed_apart():
    items = [
        {"snapshot_date": "2026-10-06", "trade_id": 7, "symbol": "ZZZ", "account_id": "A", "market_value": -300.0,
         "delta_shares": 60.0, "greeks_quality": "vendor"},
        {"snapshot_date": "2026-10-06", "trade_id": 7, "symbol": "ZZZ", "account_id": "A", "market_value": None,
         "delta_shares": None, "greeks_quality": "missing"},
        {"snapshot_date": "2026-10-06", "trade_id": None, "symbol": "YYY", "account_id": "A", "market_value": 500.0,
         "delta_shares": 10.0, "greeks_quality": None},
    ]
    out = s._trade_rollup(items)
    assert [g["trade_id"] for g in out] == [7, None]
    t7 = out[0]
    assert (t7["rows"], t7["market_value"], t7["unpriced_rows"], t7["delta_shares"], t7["rows_without_delta"]) == (
        2, -300.0, 1, 60.0, 1,
    )
    assert t7["greeks_quality"] == {"vendor": 1, "degraded": 0, "missing": 1}


@pytest.mark.parametrize(
    "row, expected",
    [
        ({"sec_type": "OPT", "option_right": "C", "strike": 280.0, "underlying_close": 360.0, "mark": 70.0}, True),
        ({"sec_type": "OPT", "option_right": "C", "strike": 280.0, "underlying_close": 360.0, "mark": 95.0}, False),
        ({"sec_type": "OPT", "option_right": "P", "strike": 50.0, "underlying_close": 40.0, "mark": 9.0}, True),
        ({"sec_type": "OPT", "option_right": "P", "strike": 50.0, "underlying_close": 60.0, "mark": 0.05}, False),
        ({"sec_type": "OPT", "option_right": "P", "strike": 50.0, "underlying_close": None, "mark": 1.0}, None),
        ({"sec_type": "STK", "mark": 1.0}, None),
    ],
)
def test_mark_below_intrinsic(row, expected):
    assert s.mark_below_intrinsic(row) is expected
