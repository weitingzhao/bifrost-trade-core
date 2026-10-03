"""TD-51 against real Postgres: paging /executions and /transactions with the keyset cursor
returns every row exactly once, in the unpaged order, through ties and NULLs.

The book has exact ties on (trade_date, exec_time) across Flex, TWS and journal rows,
microsecond-apart times, a NULL exec_time inside a dated day, NULL trade_dates with and
without an exec_time, and transactions sharing one ts. For every scope and every page size
from 1 to past the end, the pages concatenated equal the unpaged list, and the first page
equals what the reader returned before 0.40.0 for that limit.

Marked ``db`` (``make test-db``); everything runs in the fixture's transaction and is rolled
back. The env ``brokerage.*`` views are built over the ``raw_broker`` tables as in
``test_signed_qty_db.py``. Accounts, symbols and amounts are made up.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from bifrost_core.persistence.postgres.brokerage_ddl import _create_brokerage_views, ensure_brokerage_schema
from bifrost_core.portfolio.reader import executions as ex
from bifrost_core.portfolio.reader import keyset

pytestmark = pytest.mark.db

ACCOUNT = "U0000051"
D1 = date(2026, 9, 18)
D2 = date(2026, 9, 19)
D0 = date(2026, 9, 17)
T = datetime(2026, 9, 18, 15, 0, 0, 1, tzinfo=timezone.utc)
T_NEXT = T + timedelta(microseconds=1)

_VIEWS = ("executions_raw_tws", "executions_raw_flex", "executions_raw_journal", "commissions", "positions",
          "contract_quote_live", "transactions")

# (table, trade_date, exec_time, contract key suffix) -- the TWS rows have their own
# contract keys, so the on_the_fly view keeps them.
FILLS = [
    ("flex", D1, T, "A"),
    ("flex", D1, T, "A"),
    ("flex", D1, T_NEXT, "A"),
    ("flex", D1, None, "A"),
    ("flex", None, T, "A"),
    ("flex", None, None, "A"),
    ("flex", None, None, "A"),
    ("flex", D2, T, "A"),
    ("tws", D1, T, "B"),
    ("tws", D1, T, "B"),
    ("tws", None, None, "B"),
    ("tws", D2, None, "B"),
    ("journal", D1, T, "A"),
    ("journal", D0, None, "A"),
    ("journal", None, T_NEXT, "A"),
]

# ts values: three share T, two share T_NEXT, one a second earlier.
TXNS = [T, T, T, T_NEXT, T_NEXT, T - timedelta(seconds=1)]

SCOPES = ("all", "performance_book", "on_the_fly", "tws_raw")


@pytest.fixture
def book(pg_conn: Any) -> None:
    ensure_brokerage_schema(pg_conn, log=lambda m: None)
    with pg_conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS brokerage")
        for name in _VIEWS:
            cur.execute(f"CREATE OR REPLACE VIEW brokerage.{name} AS SELECT * FROM raw_broker.{name}")
        for n, (table, d, t, ck) in enumerate(FILLS):
            cur.execute(
                f"INSERT INTO raw_broker.executions_raw_{table} (exec_id, account_id, symbol, sec_type, side, "
                "quantity, price, source, contract_key, trade_date, exec_time) "
                "VALUES (%s, %s, %s, 'STK', 'BUY', 1, 10.0, %s, %s, %s, %s)",
                (f"td51.{table}.{n}", ACCOUNT, f"TDK{ck}", {"flex": "flex_trades", "tws": "tws_client",
                                                            "journal": "journal_closed"}[table],
                 f"TDK{ck}|STK|||", d, t),
            )
        for n, ts in enumerate(TXNS):
            cur.execute(
                "INSERT INTO raw_broker.transactions (account_id, ts, amount, type, report_date) "
                "VALUES (%s, %s, %s, 'Deposits/Withdrawals', DATE '2026-09-18')",
                (ACCOUNT, ts, float(n + 1)),
            )
        _create_brokerage_views(cur, "brokerage", env=True)


def _ids(rows: List[Dict[str, Any]], key: str = "account_executions_id") -> List[int]:
    return [int(r[key]) for r in rows]


def _sort_key(r: Dict[str, Any]) -> tuple:
    d, t, i = r["trade_date"], r["exec_time"], r["account_executions_id"]
    return (d is None, -(d.toordinal()) if d else 0, t is None, -(t.timestamp()) if t else 0, -i)


def _page_all_executions(conn: Any, scope: str, size: int) -> List[List[int]]:
    pages: List[List[int]] = []
    cursor: Optional[str] = None
    for _ in range(100):
        page = ex.get_executions_page(conn, account_id=ACCOUNT, limit=size, source_scope=scope, cursor=cursor)
        pages.append(_ids(page["items"]))
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
        assert len(page["items"]) == size
    raise AssertionError("paging did not end")


@pytest.mark.parametrize("scope", SCOPES)
def test_executions_pages_cover_every_row_once(pg_conn: Any, book: None, scope: str) -> None:
    full = ex.get_executions(pg_conn, account_id=ACCOUNT, limit=None, source_scope=scope)
    full_ids = _ids(full)
    assert len(full_ids) == len(set(full_ids)) >= 4
    # the unpaged order is the documented one, NULLs last on both columns
    with pg_conn.cursor() as cur:
        view = {"all": "executions", "performance_book": "executions_final", "on_the_fly": "executions_fly",
                "tws_raw": "executions_tws"}[scope]
        cur.execute(f"SELECT account_executions_id, trade_date, exec_time FROM brokerage.{view} WHERE account_id = %s",
                    (ACCOUNT,))
        stored = [dict(zip(("account_executions_id", "trade_date", "exec_time"), r)) for r in cur.fetchall()]
    assert full_ids == _ids(sorted(stored, key=_sort_key))

    for size in range(1, len(full_ids) + 2):
        pages = _page_all_executions(pg_conn, scope, size)
        flat = [i for p in pages for i in p]
        assert flat == full_ids, (scope, size)
        # the first page is what get_executions returned for this limit (no cursor = today's rows)
        first = ex.get_executions(pg_conn, account_id=ACCOUNT, limit=size, source_scope=scope)
        assert pages[0] == _ids(first), (scope, size)


def test_book_has_the_ties_and_nulls_it_claims(pg_conn: Any, book: None) -> None:
    rows = ex.get_executions(pg_conn, account_id=ACCOUNT, limit=None, source_scope="all")
    keys = [(r["trade_date"], r["time"]) for r in rows]
    on_t = [k for k in keys if k[0] == D1 and k[1] is not None and abs(k[1] - T.timestamp()) < 3e-7]
    assert len(on_t) == 5  # 2 Flex, 2 TWS, 1 journal on one exact key
    assert sum(1 for d, t in keys if d is None and t is None) == 3
    assert any(d == D1 and t is None for d, t in keys)
    assert any(d is None and t is not None for d, t in keys)


def test_executions_cursor_survives_a_new_row(pg_conn: Any, book: None) -> None:
    """A fill written between pages before the cursor does not shift the next page."""
    first = ex.get_executions_page(pg_conn, account_id=ACCOUNT, limit=3, source_scope="performance_book")
    full_before = _ids(ex.get_executions(pg_conn, account_id=ACCOUNT, limit=None, source_scope="performance_book"))
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_broker.executions_raw_flex (exec_id, account_id, symbol, sec_type, side, quantity, "
            "price, source, contract_key, trade_date, exec_time) VALUES ('td51.new', %s, 'TDKA', 'STK', 'BUY', 1, "
            "10.0, 'flex_trades', 'TDKA|STK|||', DATE '2026-09-30', TIMESTAMPTZ '2026-09-30 15:00+00')",
            (ACCOUNT,),
        )
    second = ex.get_executions_page(
        pg_conn, account_id=ACCOUNT, limit=3, source_scope="performance_book", cursor=first["next_cursor"]
    )
    assert _ids(second["items"]) == full_before[3:6]


def _page_all_transactions(conn: Any, size: int) -> List[List[int]]:
    pages: List[List[int]] = []
    cursor: Optional[str] = None
    for _ in range(100):
        page = ex.get_transactions_page(conn, account_id=ACCOUNT, limit=size, cursor=cursor)
        pages.append(_ids(page["items"], "account_transactions_id"))
        cursor = page["next_cursor"]
        if cursor is None:
            return pages
    raise AssertionError("paging did not end")


def test_transactions_pages_cover_every_row_once(pg_conn: Any, book: None) -> None:
    full = ex.get_transactions(pg_conn, account_id=ACCOUNT, limit=1000)
    full_ids = _ids(full, "account_transactions_id")
    assert len(full_ids) == len(TXNS) == len(set(full_ids))
    # ts DESC, then id DESC inside a tie
    ts_ids = [(r["ts"], r["account_transactions_id"]) for r in full]
    assert ts_ids == sorted(ts_ids, key=lambda x: (-float(x[0]), -x[1]))
    for size in range(1, len(full_ids) + 2):
        pages = _page_all_transactions(pg_conn, size)
        assert [i for p in pages for i in p] == full_ids, size
        assert pages[0] == _ids(ex.get_transactions(pg_conn, account_id=ACCOUNT, limit=size),
                                "account_transactions_id")


def test_transactions_window_and_cursor_together(pg_conn: Any, book: None) -> None:
    since = (T - timedelta(seconds=1)).timestamp() + 0.5  # drops the earliest row
    page = ex.get_transactions_page(pg_conn, since_ts=since, account_id=ACCOUNT, limit=4)
    rest = ex.get_transactions_page(pg_conn, since_ts=since, account_id=ACCOUNT, limit=4, cursor=page["next_cursor"])
    assert len(page["items"]) == 4 and len(rest["items"]) == 1 and rest["next_cursor"] is None


def test_a_cursor_from_one_list_is_refused_by_the_other(pg_conn: Any, book: None) -> None:
    page = ex.get_transactions_page(pg_conn, account_id=ACCOUNT, limit=1)
    with pytest.raises(keyset.InvalidCursor):
        ex.get_executions_page(pg_conn, account_id=ACCOUNT, limit=1, cursor=page["next_cursor"])
