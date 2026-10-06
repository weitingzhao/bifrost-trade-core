"""W4 snapshot tables against real Postgres: the DDL, capture's keep-first rule, NULL trade key.

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
        "net_liquidation double precision, total_cash double precision, buying_power double precision)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS brokerage.positions (account_id text, contract_key text, position double precision, "
        "updated_at timestamptz)"
    )
    cur.execute("INSERT INTO brokerage.account VALUES ('UZZ0001', now(), 100000, 20000, 150000)")
    cur.execute("INSERT INTO brokerage.positions VALUES ('UZZ0001', 'ZZZ|OPT|20261120|50.0|P', -3, now())")


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
    assert first == {"nav_rows": 1, "position_rows": 2, "position_rows_seen": 2}
    # Second run, different book: nothing is rewritten (the NULL-trade row included).
    attr[0]["price_last"] = 9.9
    again = daily.capture(conn, d, attribution=lambda c: attr)
    assert again["nav_rows"] == 0 and again["position_rows"] == 0
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT trade_id, trade_qty, mark, positions_updated_at IS NOT NULL FROM position_snapshot_daily "
            "WHERE snapshot_date = %s AND account_id = 'UZZ0001' ORDER BY trade_id NULLS LAST",
            (d,),
        )
        assert cur.fetchall() == [(7, -2.0, 1.3, True), (None, -1.0, 1.3, True)]
        cur.execute("SELECT net_liquidation FROM account_nav_daily WHERE snapshot_date = %s AND account_id = 'UZZ0001'", (d,))
        assert cur.fetchone() == (100000.0,)
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
                 "avg_cost": 120.0, "price_last": None, "price_mid": None, "quote_date": None,
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
