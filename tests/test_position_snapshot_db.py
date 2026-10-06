"""W4 snapshot tables against real Postgres: the DDL, capture's keep-first rule, NULL trade key,
the stale-account skip and the margin columns (0.51.0).

Marked `db`. The broker tables are stand-ins created inside the test's transaction (in DEV
they are FDW tables to Golden Source); everything is rolled back.
"""

from __future__ import annotations

from datetime import date

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
         "price_last": 1.3, "price_mid": None, "trade_id": 7, "open_qty_est": -2.0},
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
