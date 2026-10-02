"""TD-30 against real Postgres: the SQL half of the one signed-quantity rule.

1. Parity: ``signed_qty_sql`` evaluated by Postgres equals ``signed_qty`` over
   source x side x stored sign x NULL.
2. The readers on Golden Source-shaped tables: ``/executions`` quantity per scope, the
   position attribution sum (compared with the two pre-0.35.0 attribution expressions
   run on the same rows) and the option-stock link slippage (compared with the old
   link expression).

Marked ``db`` (``make test-db``). Everything runs in the fixture's transaction and is
rolled back; the ``brokerage`` names the readers use are views over the ``raw_broker``
tables created here. Accounts, symbols, ids and prices are made up.
"""

from __future__ import annotations

import itertools
from typing import Any, Dict, List

import pytest
from psycopg2.extras import RealDictCursor, execute_values

from bifrost_core.persistence.postgres.brokerage_ddl import ensure_brokerage_schema
from bifrost_core.portfolio.reader import executions as executions_reader
from bifrost_core.portfolio.reader import option_stock_link as link_reader
from bifrost_core.portfolio.signed_qty import signed_qty, signed_qty_sql

pytestmark = pytest.mark.db

ACCOUNT = "U0000002"
SOURCES = ("flex_trades", "journal_closed", "tws_client", "tws_event", None, "")
SIDES = ("BUY", "BOT", "B", "SELL", "SLD", "S", " sell ", "sld", "", None, "XYZ")
QUANTITIES = (3.5, -3.5, 0.0, None)

# The pre-0.35.0 expressions, verbatim, as oracles for "the amounts do not move".
LEGACY_LINK_QTY = (
    "CASE WHEN lower(trim(COALESCE(e.source, ''))) = 'tws_client' THEN e.quantity "
    "WHEN upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S') THEN -e.quantity "
    "ELSE e.quantity END"
)
LEGACY_FINAL_ROW = (
    "CASE WHEN lower(trim(COALESCE(e.source, ''))) = 'tws_client' THEN e.quantity "
    "WHEN upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S') THEN -abs(e.quantity) "
    "ELSE abs(e.quantity) END"
)
LEGACY_TWS_ROW = (
    "CASE WHEN upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S') THEN -abs(e.quantity) "
    "ELSE abs(e.quantity) END"
)

_VIEWS = (
    "executions", "executions_final", "executions_fly", "executions_raw_tws",
    "executions_raw_flex", "executions_raw_journal", "commissions", "positions",
    "contract_quote_live",
)


def test_sql_matches_python(pg_conn: Any) -> None:
    cases = list(itertools.product(SOURCES, SIDES, QUANTITIES))
    with pg_conn.cursor() as cur:
        rows = execute_values(
            cur,
            f"SELECT n, {signed_qty_sql('e')} FROM (VALUES %s) AS e(n, source, side, quantity) ORDER BY n",
            [(i, src, side, q) for i, (src, side, q) in enumerate(cases)],
            template="(%s, %s::text, %s::text, %s::double precision)",
            fetch=True,
        )
    assert len(rows) == len(cases)
    for (n, got), (src, side, q) in zip(rows, cases):
        assert got == signed_qty(src, side, q), (src, side, q)


@pytest.fixture
def book(pg_conn: Any) -> Dict[str, Any]:
    """Golden Source rows stored the way they are today; ``brokerage.*`` views over them."""
    ensure_brokerage_schema(pg_conn, log=lambda m: None)
    ids: Dict[str, Any] = {}
    with pg_conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS brokerage")
        for name in _VIEWS:
            cur.execute(f"CREATE OR REPLACE VIEW brokerage.{name} AS SELECT * FROM raw_broker.{name}")

        def flex(key: str, side: str, q: float, price: float, close: float, si: Any, sec: str = "STK") -> int:
            cur.execute(
                "INSERT INTO raw_broker.executions_raw_flex (exec_id, account_id, symbol, sec_type, side, "
                "quantity, price, close_price, source, contract_key, trade_date, exec_time, strategy_instance_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'flex_trades', %s, DATE '2026-09-18', "
                "TIMESTAMPTZ '2026-09-18 15:00+00', %s) RETURNING executions_raw_flex_id",
                (f"td30.f.{key}.{side}.{q}", ACCOUNT, key.split("|")[0], sec, side, q, price, close, key, si),
            )
            return cur.fetchone()[0]

        ids["buy"] = flex("TDQA|STK|||", "BUY", 300.0, 50.0, 50.5, 11)
        ids["sell"] = flex("TDQA|STK|||", "SELL", -100.0, 52.0, 50.0, 12)
        ids["opt"] = flex("TDQA|OPT|20261120|50|C", "SELL", -1.0, 1.2, 1.1, 12, sec="OPT")
        cur.execute(
            "INSERT INTO raw_broker.executions_raw_journal (account_id, symbol, sec_type, side, quantity, "
            "price, close_price, source, contract_key, trade_date, exec_time, strategy_instance_id) "
            "VALUES (%s, 'TDQA', 'STK', 'SELL', -50, 49.0, 50.0, 'journal_closed', 'TDQA|STK|||', "
            "DATE '2026-09-19', TIMESTAMPTZ '2026-09-19 15:00+00', 11) RETURNING executions_raw_journal_id",
            (ACCOUNT,),
        )
        ids["journal"] = -(1_000_000_000 + cur.fetchone()[0])
        for side, q, si in (("BOT", 40.0, 11), ("SLD", 15.0, 11), ("SLD", 5.0, 12)):
            cur.execute(
                "INSERT INTO raw_broker.executions_raw_tws (exec_id, account_id, symbol, sec_type, side, "
                "quantity, price, source, contract_key, trade_date, exec_time, strategy_instance_id) "
                "VALUES (%s, %s, 'TDQB', 'STK', %s, %s, 20.0, 'tws_client', 'TDQB|STK|||', "
                "DATE '2026-09-20', TIMESTAMPTZ '2026-09-20 15:00+00', %s)",
                (f"td30.t.{side}.{q}.{si}", ACCOUNT, side, q, si),
            )
        for key, pos in (("TDQA|STK|||", 150.0), ("TDQB|STK|||", 20.0)):
            cur.execute(
                "INSERT INTO raw_broker.positions (account_id, contract_key, symbol, sec_type, position, avg_cost) "
                "VALUES (%s, %s, %s, 'STK', %s, 10.0)",
                (ACCOUNT, key, key.split("|")[0], pos),
            )
        cur.execute(
            "INSERT INTO account_execution_option_stock_link "
            "(account_id, option_account_executions_id, stock_account_executions_id) VALUES (%s, %s, %s), (%s, %s, %s)",
            (ACCOUNT, ids["opt"], ids["sell"], ACCOUNT, ids["opt"], ids["buy"]),
        )
    return ids


def _quantities(conn: Any, scope: str) -> Dict[tuple, float]:
    rows = executions_reader.get_executions(conn, account_id=ACCOUNT, limit=None, source_scope=scope)
    return {(r["source"], r["symbol"], r["side"], abs(r["quantity"])): r["quantity"] for r in rows}


def test_executions_quantity_per_scope(pg_conn: Any, book: Dict[str, Any]) -> None:
    signed = {
        ("flex_trades", "TDQA", "BUY", 300.0): 300.0,
        ("flex_trades", "TDQA", "SELL", 100.0): -100.0,  # was +100 before 0.35.0
        ("flex_trades", "TDQA", "SELL", 1.0): -1.0,  # the option leg
        ("journal_closed", "TDQA", "SELL", 50.0): -50.0,  # was +50
    }
    tws_signed = {
        ("tws_client", "TDQB", "BOT", 40.0): 40.0,
        ("tws_client", "TDQB", "SLD", 15.0): -15.0,  # was +15
        ("tws_client", "TDQB", "SLD", 5.0): -5.0,  # was +5
    }
    assert _quantities(pg_conn, "performance_book") == signed
    assert _quantities(pg_conn, "all") == {**signed, **tws_signed}
    assert _quantities(pg_conn, "on_the_fly") == tws_signed
    # tws_raw shows the stored value
    assert _quantities(pg_conn, "tws_raw") == {k: abs(v) for k, v in tws_signed.items()}


def _legacy_attribution(conn: Any) -> Dict[tuple, float]:
    """SUM of the pre-0.35.0 row expressions per (position, instance), for direct ids."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT e.contract_key, e.strategy_instance_id, SUM({LEGACY_FINAL_ROW})
            FROM brokerage.executions_final e WHERE e.account_id = %s AND e.sec_type = 'STK'
            GROUP BY 1, 2
            UNION ALL
            SELECT e.contract_key, e.strategy_instance_id, SUM({LEGACY_TWS_ROW})
            FROM brokerage.executions_raw_tws e WHERE e.account_id = %s
            GROUP BY 1, 2
            """,
            (ACCOUNT, ACCOUNT),
        )
        return {(k, si): float(v) for k, si, v in cur.fetchall()}


def test_attribution_sum_is_unchanged(pg_conn: Any, book: Dict[str, Any]) -> None:
    rows = executions_reader.get_position_instance_attribution(pg_conn, account_id=ACCOUNT)
    got = {
        (r["contract_key"], r["strategy_instance_id"]): float(r["open_qty_est"])
        for r in rows
        if r.get("strategy_instance_id") is not None
    }
    assert got == _legacy_attribution(pg_conn)
    assert got == {
        ("TDQA|STK|||", 11): 250.0,  # +300 Flex buy, -50 journal sell
        ("TDQA|STK|||", 12): -100.0,  # Flex sell
        ("TDQB|STK|||", 11): 25.0,  # +40 BOT, -15 SLD (TWS only: no final row for TDQB)
        ("TDQB|STK|||", 12): -5.0,
    }


def test_option_stock_link_slippage_is_unchanged(pg_conn: Any, book: Dict[str, Any]) -> None:
    res = link_reader.get_option_stock_links(pg_conn, ACCOUNT, book["opt"])
    with pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT e.account_executions_id AS id, {LEGACY_LINK_QTY} AS q, e.price, e.close_price
            FROM account_execution_option_stock_link l
            JOIN brokerage.executions_final e
              ON e.account_executions_id = l.stock_account_executions_id AND e.account_id = l.account_id
            WHERE l.option_account_executions_id = %s
            """,
            (book["opt"],),
        )
        legacy: List[Dict[str, Any]] = [dict(r) for r in cur.fetchall()]
    old = {r["id"]: float(r["q"]) * (float(r["price"]) - float(r["close_price"])) for r in legacy}
    new = {r["stock_account_executions_id"]: r["slippage_vs_close"] for r in res["links"]}
    assert new == pytest.approx(old)
    assert res["slippage_total"] == pytest.approx(sum(old.values()))
    assert {r["stock_account_executions_id"]: r["stock_quantity"] for r in res["links"]} == {
        book["sell"]: -100.0,  # was +100
        book["buy"]: 300.0,
    }
