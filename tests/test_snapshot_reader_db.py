"""The snapshot reader against real Postgres (TD-138 / TD-139, core 0.54.0).

Marked `db`. Rows are invented and written inside the test's transaction; everything is rolled
back. The reader's own rollbacks are swallowed by a wrapper so they cannot drop the seed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from bifrost_core.portfolio.reader import snapshots
from bifrost_core.portfolio.snapshot import daily

pytestmark = pytest.mark.db


class _NoRollback:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, **kw):
        return self._conn.cursor(**kw)

    def rollback(self):
        return None


def _seed(cur):
    # 16:00 New York is 20:00 UTC on these dates. 10-05 UZZ0002 was read at 11:48 New York (dropped).
    cur.execute(
        "INSERT INTO account_nav_daily (snapshot_date, account_id, net_liquidation, total_cash, buying_power, "
        "cushion, account_updated_at) VALUES "
        "('2026-10-02', 'UZZ0001', 1000, 100, 1500, NULL, '2026-10-02 20:05+00'), "
        "('2026-10-05', 'UZZ0001', 1010, 100, 1500, NULL, '2026-10-05 20:05+00'), "
        "('2026-10-05', 'UZZ0002', 500, 500, 500, NULL, '2026-10-05 15:48+00'), "
        "('2026-10-06', 'UZZ0001', 1020, 100, 1500, 0.4, '2026-10-06 20:05+00'), "
        "('2026-10-06', 'UZZ0002', 505, 500, 500, 0.9, '2026-10-06 20:01+00')"
    )
    opt = "ZZZ|OPT|20261120|50.0|P"
    cur.execute(
        "INSERT INTO position_snapshot_daily (snapshot_date, account_id, contract_key, trade_id, symbol, sec_type, "
        "expiry, strike, option_right, position_qty, trade_qty, mark, mark_source, underlying_close, delta, gamma, "
        "vega, theta, iv, greeks_asof) VALUES "
        # 10-05: one vendor option row for trade 7, the unattributed remainder with no vendor row, a stock.
        "('2026-10-05', 'UZZ0001', %(o)s, 7, 'ZZZ', 'OPT', '2026-11-20', 50, 'P', -3, -2, 1.5, 'vendor_eod', 50, "
        " -0.3, 0.04, 0.08, -0.02, 0.40, '2026-10-05 20:00+00'), "
        "('2026-10-05', 'UZZ0001', %(o)s, NULL, 'ZZZ', 'OPT', '2026-11-20', 50, 'P', -3, -1, 1.5, 'vendor_eod', 50, "
        " NULL, NULL, NULL, NULL, NULL, NULL), "
        "('2026-10-05', 'UZZ0002', 'YYY|STK|||', NULL, 'YYY', 'STK', NULL, NULL, NULL, 10, 10, 20, 'vendor_eod', 20, "
        " NULL, NULL, NULL, NULL, NULL, NULL), "
        # 10-06: the option again (Greeks a day old on the trade row: degraded), the stock, a new stock.
        "('2026-10-06', 'UZZ0001', %(o)s, 7, 'ZZZ', 'OPT', '2026-11-20', 50, 'P', -3, -2, 1.2, 'vendor_eod', 51, "
        " -0.28, 0.04, 0.08, -0.02, 0.38, '2026-10-05 20:00+00'), "
        "('2026-10-06', 'UZZ0001', %(o)s, NULL, 'ZZZ', 'OPT', '2026-11-20', 50, 'P', -3, -1, 1.2, 'vendor_eod', 51, "
        " NULL, NULL, NULL, NULL, NULL, NULL), "
        "('2026-10-06', 'UZZ0002', 'YYY|STK|||', NULL, 'YYY', 'STK', NULL, NULL, NULL, 10, 10, 21, 'vendor_eod', 21, "
        " NULL, NULL, NULL, NULL, NULL, NULL), "
        "('2026-10-06', 'UZZ0002', 'XXX|STK|||', 9, 'XXX', 'STK', NULL, NULL, NULL, 5, 5, 30, 'vendor_eod', 30, "
        " NULL, NULL, NULL, NULL, NULL, NULL)",
        {"o": opt},
    )


def test_nav_history_drops_rows_read_before_the_close(pg_conn):
    with pg_conn.cursor() as cur:
        _seed(cur)
    out = snapshots.nav_history(_NoRollback(pg_conn), from_date=date(2026, 10, 5), to_date=date(2026, 10, 6))
    assert [(i["snapshot_date"], i["account_id"]) for i in out["items"]] == [
        ("2026-10-05", "UZZ0001"),
        ("2026-10-06", "UZZ0001"),
        ("2026-10-06", "UZZ0002"),
    ]
    assert [(d["snapshot_date"], d["account_id"]) for d in out["dropped"]] == [("2026-10-05", "UZZ0002")]
    assert out["sessions"] == ["2026-10-05", "2026-10-06"]
    assert out["items"][-1]["cushion"] == 0.9 and out["items"][0]["cushion"] is None
    one = snapshots.nav_history(_NoRollback(pg_conn), account_id="UZZ0002")
    assert [i["snapshot_date"] for i in one["items"]] == ["2026-10-06"]
    pg_conn.rollback()


def test_session_closes_agree_with_the_single_date_helper(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS market")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS market.us_market_holiday (exchange text, holiday_date date, name text, "
            "status text, open_time timestamptz, close_time timestamptz)"
        )
        cur.execute(
            "INSERT INTO market.us_market_holiday VALUES ('NYSE', '2026-11-27', 'Day after Thanksgiving', "
            "'early-close', '2026-11-27 14:30+00', '2026-11-27 18:00+00')"
        )
    days = [date(2026, 11, 27), date(2026, 11, 30), date(2026, 10, 5)]
    many = daily.session_closes_at(pg_conn, days)
    assert many == {d: daily.session_close_at(pg_conn, d) for d in days}
    assert many[date(2026, 11, 27)] == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    pg_conn.rollback()


def test_position_snapshots_latest_session_quality_and_trade_rollup(pg_conn):
    with pg_conn.cursor() as cur:
        _seed(cur)
    out = snapshots.position_snapshots(_NoRollback(pg_conn))
    assert out["sessions"] == ["2026-10-06"]
    opt = [i for i in out["items"] if i["sec_type"] == "OPT"]
    assert sorted((i["trade_id"] or 0, i["greeks_quality"]) for i in opt) == [(0, "missing"), (7, "degraded")]
    # TD-139 acceptance: missing == OPT rows with no delta in the session.
    assert out["greeks_quality"]["2026-10-06"] == {"vendor": 0, "degraded": 1, "missing": 1}
    assert all(i["greeks_quality"] is None for i in out["items"] if i["sec_type"] == "STK")
    assert [t["trade_id"] for t in out["trades"]] == [7, 9, None]
    t7 = out["trades"][0]
    assert t7["market_value"] == pytest.approx(-2 * 100 * 1.2) and t7["symbols"] == ["ZZZ"]
    unattributed = out["trades"][-1]
    assert unattributed["rows"] == 2 and unattributed["rows_without_delta"] == 1  # the option remainder
    fresh = {(i["account_id"]) : i["account_fresh_at_close"] for i in out["items"]}
    assert fresh == {"UZZ0001": True, "UZZ0002": True}
    old = snapshots.position_snapshots(_NoRollback(pg_conn), from_date=date(2026, 10, 5), to_date=date(2026, 10, 5))
    assert {i["account_id"]: i["account_fresh_at_close"] for i in old["items"]}["UZZ0002"] is False
    pg_conn.rollback()


def test_pnl_attribution_differences_a_session_against_its_prior_one(pg_conn):
    with pg_conn.cursor() as cur:
        _seed(cur)
    out = snapshots.pnl_attribution(_NoRollback(pg_conn), from_date=date(2026, 10, 5), to_date=date(2026, 10, 6))
    sessions = {s["snapshot_date"]: s for s in out["sessions"]}
    # 10-05's prior session is Friday 10-02: no position snapshot then -> no reading, never an older day.
    assert sessions["2026-10-05"]["status"] == "no_prior_snapshot" and sessions["2026-10-05"]["prior_date"] == "2026-10-02"
    s6 = sessions["2026-10-06"]
    assert s6["status"] == "ok" and s6["prior_date"] == "2026-10-05" and s6["days"] == 1
    rows = {(r["contract_key"], r["trade_id"]): r for r in out["items"]}
    t7 = rows[("ZZZ|OPT|20261120|50.0|P", 7)]
    assert t7["status"] == "ok" and t7["greeks_quality"] == "vendor"  # the prior close's Greeks were that session's
    assert t7["delta_pnl"] == pytest.approx(-200 * -0.3 * 1.0)
    rest = rows[("ZZZ|OPT|20261120|50.0|P", None)]
    assert rest["greeks_quality"] == "missing" and rest["unexplained"] is None
    assert rest["held_pnl"] == pytest.approx(-100 * (1.2 - 1.5))
    assert rows[("XXX|STK|||", 9)]["status"] == "opened_in_session"
    totals = s6["totals"]
    assert totals["read_rows"] == 2 and totals["unread_rows"] == 2
    parts = sum(totals[k] for k in ("delta_pnl", "gamma_pnl", "vega_pnl", "theta_pnl"))
    assert totals["held_pnl"] == pytest.approx(parts + totals["unexplained"])
    assert {g["trade_id"] for g in out["by_trade"]} == {7, 9, None}
    only7 = snapshots.pnl_attribution(_NoRollback(pg_conn), from_date=date(2026, 10, 6), to_date=date(2026, 10, 6), trade_id=7)
    assert [(r["trade_id"], r["status"]) for r in only7["items"]] == [(7, "ok")]
    pg_conn.rollback()
