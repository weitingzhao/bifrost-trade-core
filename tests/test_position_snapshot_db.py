"""W4 snapshot tables against real Postgres: the DDL, capture's keep-first rule, NULL trade key,
the stale-account skip and the margin columns (0.52.0).

Marked `db`. The broker tables are stand-ins created inside the test's transaction (in DEV
they are FDW tables to Golden Source); everything is rolled back.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from bifrost_core.portfolio.snapshot import daily

pytestmark = pytest.mark.db


class _NoCommit:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, **kw):
        return self._conn.cursor(**kw)

    def commit(self):
        return None

    def rollback(self):
        return None


def _stand_in_broker(cur):
    cur.execute("CREATE SCHEMA IF NOT EXISTS brokerage")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS brokerage.account (account_id text PRIMARY KEY, updated_at timestamptz, "
        "net_liquidation double precision, total_cash double precision, buying_power double precision, "
        "summary_extra jsonb)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS brokerage.positions (account_id text, contract_key text, position double precision, "
        "updated_at timestamptz)"
    )
    # 16:00 New York on 2026-10-05 is 20:00 UTC. UZZ0001 synced after it; UZZ0002 last synced at 11:48 New York.
    cur.execute(
        "INSERT INTO brokerage.account VALUES ('UZZ0001', '2026-10-05 20:10+00', 100000, 20000, 150000, "
        """'{"Cushion": "0.25", "ExcessLiquidity": "25000", "MaintMarginReq": "Infinity"}')"""
    )
    cur.execute("INSERT INTO brokerage.account VALUES ('UZZ0002', '2026-10-05 15:48+00', 5000, 5000, 5000, NULL)")
    cur.execute(
        "INSERT INTO brokerage.positions VALUES ('UZZ0001', 'ZZZ|OPT|20261120|50.0|P', -3, '2026-10-05 20:10+00')"
    )
    cur.execute("INSERT INTO brokerage.positions VALUES ('UZZ0002', 'YYY|STK|||', 10, '2026-10-05 15:48+00')")


def test_capture_keeps_the_first_rows_of_a_day(pg_conn):
    conn = _NoCommit(pg_conn)
    with pg_conn.cursor() as cur:
        _stand_in_broker(cur)
    d = date(2026, 10, 5)
    attr = [
        {"account_id": "UZZ0001", "contract_key": "ZZZ|OPT|20261120|50.0|P", "symbol": "ZZZ", "sec_type": "OPT",
         "expiry": "20261120", "strike": 50.0, "option_right": "P", "position_qty": -3.0, "avg_cost": 120.0,
         "price_last": 1.3, "price_mid": None, "mark_source": "quote_live", "trade_id": 7, "open_qty_est": -2.0},
    ]
    first = daily.capture(conn, d, attribution=lambda c: attr)
    assert (first["nav_rows"], first["position_rows"], first["position_rows_seen"]) == (1, 2, 2)
    assert first["stale_accounts"] == [{"account_id": "UZZ0002", "updated_at": "2026-10-05T15:48:00+00:00"}]
    # Second run, different book: nothing is rewritten (the NULL-trade row included).
    attr[0]["price_last"] = 9.9
    again = daily.capture(conn, d, attribution=lambda c: attr)
    assert again["nav_rows"] == 0 and again["position_rows"] == 0
    assert again["already_captured"] == ["UZZ0001"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT trade_id, trade_qty, mark, positions_updated_at IS NOT NULL FROM position_snapshot_daily "
            "WHERE snapshot_date = %s AND account_id = 'UZZ0001' ORDER BY trade_id NULLS LAST",
            (d,),
        )
        assert cur.fetchall() == [(7, -2.0, 1.3, True), (None, -1.0, 1.3, True)]
        cur.execute(
            "SELECT account_id, net_liquidation, cushion, excess_liquidity, maint_margin_req FROM account_nav_daily "
            "WHERE snapshot_date = %s AND account_id LIKE 'UZZ%%' ORDER BY account_id",
            (d,),
        )
        assert cur.fetchall() == [("UZZ0001", 100000.0, 0.25, 25000.0, None)]  # UZZ0002 was stale
        cur.execute("SELECT count(*) FROM position_snapshot_daily WHERE account_id = 'UZZ0002'")
        assert cur.fetchone() == (0,)
    pg_conn.rollback()


def test_session_close_is_four_pm_new_york(pg_conn):
    from datetime import datetime, timezone

    close = daily.session_close_at(pg_conn, date(2026, 10, 5))
    assert close == datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
    pg_conn.rollback()


def test_session_close_reads_an_early_close(pg_conn):
    from datetime import datetime, timezone

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
    assert daily.session_close_at(pg_conn, date(2026, 11, 27)) == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    assert daily.session_close_at(pg_conn, date(2026, 11, 30)) == datetime(2026, 11, 30, 21, 0, tzinfo=timezone.utc)
    pg_conn.rollback()


def test_margin_columns_are_additive_and_idempotent(pg_conn):
    from bifrost_core.persistence.postgres.snapshot_ddl import ensure_snapshot_tables

    with pg_conn.cursor() as cur:
        ensure_snapshot_tables(cur)  # a second run (the fixture ran the first) is a no-op
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'account_nav_daily' AND column_name IN "
            "('cushion', 'excess_liquidity', 'maint_margin_req') ORDER BY column_name"
        )
        assert cur.fetchall() == [
            ("cushion", "double precision", "YES"),
            ("excess_liquidity", "double precision", "YES"),
            ("maint_margin_req", "double precision", "YES"),
        ]
    pg_conn.rollback()


def test_attribution_reads_the_newest_vendor_eod_mark_and_capture_does_not_keep_it(pg_conn):
    """TD-140: the fallback reads the snapshot, and the snapshot never takes it back as its own mark."""
    from bifrost_core.portfolio.reader.executions import _vendor_eod_snapshot_marks, label_marks

    conn = _NoCommit(pg_conn)
    key = "ZZZ|OPT|20261120|50.0|P"
    with pg_conn.cursor() as cur:
        _stand_in_broker(cur)
        cur.execute(
            "INSERT INTO position_snapshot_daily (snapshot_date, account_id, contract_key, trade_id, sec_type, "
            "position_qty, trade_qty, mark, mark_source) VALUES "
            "('2026-10-01', 'UZZ0001', %(k)s, 7, 'OPT', -3, -3, 1.10, 'vendor_eod'), "
            "('2026-10-02', 'UZZ0001', %(k)s, 7, 'OPT', -3, -3, 1.20, 'vendor_eod'), "
            "('2026-10-03', 'UZZ0001', %(k)s, 7, 'OPT', -3, -3, 9.99, 'quote_live'), "
            "('2026-10-04', 'UZZ0001', %(k)s, 7, 'OPT', -3, -3, NULL, NULL)",
            {"k": key},
        )
    assert _vendor_eod_snapshot_marks(conn, [key, "ZZZ|STK|||"]) == {key: (1.2, date(2026, 10, 2))}

    def reader_with_fallback(c):
        rows = [{"account_id": "UZZ0001", "contract_key": key, "symbol": "ZZZ", "sec_type": "OPT",
                 "expiry": "20261120", "strike": 50.0, "option_right": "P", "position_qty": -3.0,
                 "avg_cost": 120.0,
                 "trade_id": 7, "open_qty_est": -3.0}]
        label_marks(c, rows, stock_closes=lambda s: {})
        assert (rows[0]["price_last"], rows[0]["mark_source"]) == (1.2, "vendor_eod")
        return rows

    d = date(2026, 10, 5)
    daily.capture(conn, d, attribution=reader_with_fallback)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT mark, mark_source FROM position_snapshot_daily WHERE snapshot_date = %s AND contract_key = %s",
            (d, key),
        )
        assert cur.fetchall() == [(None, None)]  # left for enrich to fill with 10-05's own close
    pg_conn.rollback()


def test_enrich_replaces_a_close_under_intrinsic_and_the_fallback_reads_it(pg_conn):
    """TD-246 against the real table: enrich stores the vendor-IV price with its own label, and
    the attribution fallback takes that session's mark over an older vendor close."""
    from bifrost_core.portfolio.reader.executions import _vendor_eod_snapshot_marks

    conn = _NoCommit(pg_conn)
    key = "ZZZ|OPT|20270115|280.0|C"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO position_snapshot_daily (snapshot_date, account_id, contract_key, trade_id, symbol, "
            "sec_type, expiry, strike, option_right, position_qty, trade_qty, mark, mark_source) VALUES "
            "('2026-10-02', 'UZZ0001', %(k)s, 7, 'ZZZ', 'OPT', '2027-01-15', 280, 'C', 1, 1, 70.0, 'vendor_eod'), "
            "('2026-10-05', 'UZZ0001', %(k)s, 7, 'ZZZ', 'OPT', '2027-01-15', 280, 'C', 1, 1, NULL, NULL)",
            {"k": key},
        )
    chain = [{"option_ticker": "O:ZZZ270115C00280000", "delta": 0.85, "gamma": 0.002, "vega": 0.4,
              "theta": -0.08, "iv": 0.65, "day_close": 70.0, "snapshot_ts": "2026-10-05T20:00:00Z"}]
    out = daily.enrich(conn, date(2026, 10, 5), option_rows=lambda s, e, a: chain,
                       closes=lambda syms, as_of: {"ZZZ": {"bar_time": 1791158400.0, "close": 370.0}})
    assert out["updated"] == 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT mark, mark_source, underlying_close FROM position_snapshot_daily "
            "WHERE snapshot_date = '2026-10-05' AND contract_key = %s",
            (key,),
        )
        mark, source, under = cur.fetchone()
    assert source == "vendor_iv_model" and under == 370.0 and mark > 90.0  # intrinsic 90
    # The fallback reads the newest end-of-day mark, not the stale 70 of 10-02 ...
    assert _vendor_eod_snapshot_marks(conn, [key]) == {key: (mark, date(2026, 10, 5))}
    # ... and a second enrich leaves the stored mark alone (only NULLs are filled).
    daily.enrich(conn, date(2026, 10, 5), option_rows=lambda s, e, a: chain,
                 closes=lambda syms, as_of: {"ZZZ": {"bar_time": 1791158400.0, "close": 370.0}})
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM position_snapshot_daily WHERE contract_key = %s AND mark_source = 'vendor_iv_model'",
            (key,),
        )
        assert cur.fetchone()[0] == 1
    pg_conn.rollback()


def test_session_bounds_are_new_york_midnight_and_close(pg_conn):
    """TD-250: the bounds a last trade is judged against, by the database clock."""
    start, close = daily.session_bounds_at(pg_conn, date(2026, 10, 5))
    assert start == datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
    assert close == datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
    pg_conn.rollback()


def test_enrich_replaces_a_morning_close_far_from_the_vendor_iv_price(pg_conn):
    """TD-250 against the real table and the real bounds: a close over intrinsic but traded in the
    morning, a third over the vendor-IV price, is stored as that price; one traded at the close
    is kept as the vendor's close."""
    conn = _NoCommit(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO position_snapshot_daily (snapshot_date, account_id, contract_key, trade_id, symbol, "
            "sec_type, expiry, strike, option_right, position_qty, trade_qty, mark, mark_source) VALUES "
            "('2026-10-05', 'UZZ0001', 'ZZZ|OPT|20270115|280.0|C', 7, 'ZZZ', 'OPT', '2027-01-15', 280, 'C', "
            " 1, 1, NULL, NULL), "
            "('2026-10-05', 'UZZ0001', 'ZZZ|OPT|20270115|290.0|C', 7, 'ZZZ', 'OPT', '2027-01-15', 290, 'C', "
            " 1, 1, NULL, NULL)"
        )
    chain = [
        {"option_ticker": "O:ZZZ270115C00280000", "delta": 0.85, "gamma": 0.002, "vega": 0.4, "theta": -0.08,
         "iv": 0.65, "day_close": 140.0, "snapshot_ts": "2026-10-05T20:00:00Z",
         "last_trade_ts": "2026-10-05T13:48:03.112+00:00"},
        {"option_ticker": "O:ZZZ270115C00290000", "delta": 0.8, "gamma": 0.002, "vega": 0.4, "theta": -0.08,
         "iv": 0.65, "day_close": 140.0, "snapshot_ts": "2026-10-05T20:00:00Z",
         "last_trade_ts": "2026-10-05T20:15:03.216+00:00"},
    ]
    out = daily.enrich(conn, date(2026, 10, 5), option_rows=lambda s, e, a: chain,
                       closes=lambda syms, as_of: {"ZZZ": {"bar_time": 1791158400.0, "close": 370.0}})
    assert out["updated"] == 2
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT strike, mark, mark_source FROM position_snapshot_daily "
            "WHERE snapshot_date = '2026-10-05' AND symbol = 'ZZZ' ORDER BY strike"
        )
        (_, m280, s280), (_, m290, s290) = cur.fetchall()
    assert s280 == "vendor_iv_model" and 90.0 < m280 < 140.0 / 1.2
    assert (m290, s290) == (140.0, "vendor_eod")
    pg_conn.rollback()
