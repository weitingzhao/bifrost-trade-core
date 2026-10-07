"""TD-114 / TD-115 against real Postgres: the money writers and the commission sign.

Covers what had no test before core 0.50.0:

* ``write_account_executions_to_db``'s Flex branches -- the synthetic ``flex_<account>_<tradeID>``
  exec id, the ``executions_raw_flex`` conflict update, the commission keep-non-zero upsert;
* ``upsert_account_transactions`` -- the happy path (stored fields, the conflict update, skipped
  rows) and a failed write (nothing lands, no rows reported written);
* the commission sign (TD-114): every writer stores IB's statement sign (charge negative) and the
  ledger reader returns a cost (positive) whatever the execution's source.

Marked ``db`` (``make test-db``). Golden Source's ``raw_broker`` tables live in the same database;
``brokerage`` holds pass-through views standing in for the FDW tables. Everything runs in the
fixture's transaction and is rolled back. Accounts, symbols, ids, prices and amounts are made up.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.persistence.postgres.brokerage_ddl import _create_brokerage_views, ensure_brokerage_schema
from bifrost_core.portfolio.reader import accounts
from bifrost_core.portfolio.reader import executions as executions_reader

pytestmark = pytest.mark.db

ACCT = "U0000009"
CFG = {"sink": "postgres"}
PASS_THROUGH = ("executions_raw_flex", "executions_raw_tws", "executions_raw_journal", "commissions", "transactions")


class _Savepointed:
    """The fixture's connection with commit / rollback mapped onto one savepoint."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._run("SAVEPOINT td115")

    def _run(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def cursor(self, **kw: Any) -> Any:
        return self._conn.cursor(**kw)

    def commit(self) -> None:
        self._run("RELEASE SAVEPOINT td115")
        self._run("SAVEPOINT td115")

    def rollback(self) -> None:
        self._run("ROLLBACK TO SAVEPOINT td115")

    def close(self) -> None:
        return None


@pytest.fixture
def db(pg_conn, monkeypatch: pytest.MonkeyPatch) -> _Savepointed:
    conn = _Savepointed(pg_conn)
    ensure_brokerage_schema(conn, log=lambda m: None)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS brokerage CASCADE")
        cur.execute("CREATE SCHEMA brokerage")
        for t in PASS_THROUGH:
            cur.execute(f"CREATE VIEW brokerage.{t} AS SELECT * FROM raw_broker.{t}")
        _create_brokerage_views(cur, "brokerage", env=True)
    conn.commit()
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: conn)
    return conn


def _all(db: _Savepointed, sql: str, params: Any = None) -> List[tuple]:
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _stored_commission(db: _Savepointed, exec_id: str) -> Any:
    rows = _all(db, "SELECT commission FROM raw_broker.commissions WHERE exec_id = %s", (exec_id,))
    assert len(rows) == 1, rows
    return rows[0][0]


def _read_commissions(db: _Savepointed) -> Dict[str, Any]:
    rows = executions_reader.get_executions(db, account_id=ACCT, limit=None, source_scope="all")
    return {r["exec_id"]: r["commission"] for r in rows}


def _flex_row(trade_id: str, **over: Any) -> Dict[str, Any]:
    """One parsed Flex Trades row (the plugin's parse_trades_xml shape), made-up values."""
    row: Dict[str, Any] = {
        "account_id": ACCT,
        "exec_id": None,  # BookTrade rows carry no ibExecID
        "trade_id": trade_id,
        "source": "flex_trades",
        "time": 1790000000.0,
        "trade_date": "2026-09-21",
        "report_date": "2026-09-21",
        "symbol": "ZQX   261120C00050000",
        "sec_type": "OPT",
        "side": "SELL",
        "quantity": -1.0,
        "price": 1.25,
        "contract_key": "ZQX|OPT|20261120|50|C",
        "proceeds": 125.0,
        "net_cash": 124.35,
        "taxes": 0.0,
        "commission": -0.65,
        "currency": "USD",
    }
    row.update(over)
    return row


# --- write_account_executions_to_db: the Flex branches -----------------------------------------


def test_flex_row_without_exec_id_gets_the_synthetic_id(db) -> None:
    assert accounts.write_account_executions_to_db(CFG, [_flex_row("7001")])
    rows = _all(db, "SELECT exec_id, trade_id, quantity FROM raw_broker.executions_raw_flex WHERE account_id = %s",
                (ACCT,))
    assert rows == [(f"flex_{ACCT}_7001", "7001", -1.0)]


def test_flex_rewrite_updates_the_row_in_place(db) -> None:
    assert accounts.write_account_executions_to_db(CFG, [_flex_row("7002", price=1.25)])
    # A later statement corrects the price and the time: one row, the new values.
    assert accounts.write_account_executions_to_db(CFG, [_flex_row("7002", price=1.3, time=1790000600.0)])
    rows = _all(db, "SELECT price, extract(epoch FROM exec_time) FROM raw_broker.executions_raw_flex "
                    "WHERE exec_id = %s", (f"flex_{ACCT}_7002",))
    assert rows == [(1.3, 1790000600)]


def test_flex_commission_keeps_a_non_zero_value_against_a_zero(db) -> None:
    eid = f"flex_{ACCT}_7003"
    assert accounts.write_account_executions_to_db(CFG, [_flex_row("7003", commission=-0.65)])
    assert _stored_commission(db, eid) == -0.65
    # A wider re-pull that carries 0 does not erase it ...
    assert accounts.write_account_executions_to_db(CFG, [_flex_row("7003", commission=0)])
    assert _stored_commission(db, eid) == -0.65
    # ... a different non-zero value replaces it.
    assert accounts.write_account_executions_to_db(CFG, [_flex_row("7003", commission=-0.7)])
    assert _stored_commission(db, eid) == -0.7


def test_flex_rebate_is_stored_positive(db) -> None:
    assert accounts.write_account_executions_to_db(CFG, [_flex_row("7004", commission=0.12)])
    assert _stored_commission(db, f"flex_{ACCT}_7004") == 0.12


# --- TD-114: one stored sign, cost-positive reads ----------------------------------------------


def _tws_row(exec_id: str, commission: Any) -> Dict[str, Any]:
    return {
        "account_id": ACCT,
        "exec_id": exec_id,
        "source": "tws_client",
        "time": 1790000000.0,
        "symbol": "ZQY",
        "sec_type": "STK",
        "side": "BOT",
        "quantity": 10.0,
        "price": 20.0,
        "contract_key": "ZQY|STK|||",
        "commission": commission,
        "currency": "USD",
    }


def test_tws_commission_report_is_stored_as_a_charge(db) -> None:
    assert accounts.write_account_executions_to_db(CFG, [_tws_row("td114.tws.1", 1.05)])
    assert _stored_commission(db, "td114.tws.1") == -1.05
    assert _read_commissions(db)["td114.tws.1"] == 1.05


def test_late_tws_report_on_a_flex_fill_keeps_the_flex_sign(db) -> None:
    """The exec id is shared: TWS reporting after Flex used to flip the row to a positive "rebate"."""
    row = _flex_row("7005", exec_id="td114.shared.1", commission=-1.05)
    assert accounts.write_account_executions_to_db(CFG, [row])
    assert accounts.update_execution_commission(CFG, "td114.shared.1", 1.05, None, "USD")
    assert _stored_commission(db, "td114.shared.1") == -1.05
    assert _read_commissions(db)["td114.shared.1"] == 1.05


def test_reader_returns_a_cost_for_every_source(db) -> None:
    assert accounts.write_account_executions_to_db(
        CFG, [_flex_row("7006", exec_id="td114.flex.1", commission=-0.65), _tws_row("td114.tws.2", 1.0)]
    )
    jid = accounts.insert_one_execution(
        CFG,
        {"account_id": ACCT, "exec_id": "td114.journal.1", "symbol": "ZQY", "quantity": -5, "price": 21.0,
         "side": "SELL", "source": "journal_closed", "time": 1790000100.0, "commission": 1.5},
    )
    mid = accounts.insert_one_execution(
        CFG,
        {"account_id": ACCT, "exec_id": "td114.manual.1", "symbol": "ZQY", "quantity": 5, "price": 19.0,
         "side": "BUY", "time": 1790000200.0, "commission": 0.5},
    )
    assert jid is not None and mid is not None
    assert {k: _stored_commission(db, k) for k in ("td114.journal.1", "td114.manual.1")} == {
        "td114.journal.1": -1.5,
        "td114.manual.1": -0.5,
    }
    got = _read_commissions(db)
    assert {k: got[k] for k in ("td114.flex.1", "td114.tws.2", "td114.journal.1", "td114.manual.1")} == {
        "td114.flex.1": 0.65,
        "td114.tws.2": 1.0,
        "td114.journal.1": 1.5,
        "td114.manual.1": 0.5,
    }


def test_ledger_edit_of_a_flex_fill_round_trips(db) -> None:
    """The form sends back what the reader showed (a cost); the Flex row must keep its sign."""
    assert accounts.write_account_executions_to_db(CFG, [_flex_row("7007", exec_id="td114.flex.2", commission=-0.65)])
    (raw_id,) = _all(db, "SELECT executions_raw_flex_id FROM raw_broker.executions_raw_flex WHERE exec_id = %s",
                     ("td114.flex.2",))[0]
    shown = _read_commissions(db)["td114.flex.2"]
    assert shown == 0.65
    assert accounts.update_one_execution(CFG, raw_id, {"price": 1.26, "commission": shown})
    assert _stored_commission(db, "td114.flex.2") == -0.65
    assert _read_commissions(db)["td114.flex.2"] == 0.65
    # A typed 0 by hand is stored (edits are not re-pulls).
    assert accounts.update_one_execution(CFG, raw_id, {"commission": 0})
    assert _stored_commission(db, "td114.flex.2") == 0


# --- upsert_account_transactions --------------------------------------------------------------


def _cash(**over: Any) -> Dict[str, Any]:
    """One parsed CashTransaction (the plugin's parse_cash_transactions_xml shape), made-up values."""
    row: Dict[str, Any] = {
        "account_id": ACCT,
        "ts": 1789963200.0,  # 2026-09-21 04:00 UTC
        "amount": 12.34,
        "type": "dividend",
        "currency": "USD",
        "description": "ZQY CASH DIVIDEND",
        "flex_transaction_id": "990001",
        "flex_type": "Dividends",
        "symbol": "ZQY",
        "conid": "123456",
        "report_date": "20260921",
        "raw_extra": {"transactionID": "990001"},
    }
    row.update(over)
    return row


def _cash_rows(db: _Savepointed) -> List[tuple]:
    return _all(
        db,
        "SELECT amount, type, currency, description, flex_transaction_id, conid, report_date::text, "
        "raw_extra->>'transactionID' FROM raw_broker.transactions WHERE account_id = %s ORDER BY amount",
        (ACCT,),
    )


def test_cash_rows_are_stored(db) -> None:
    rows = [_cash(), _cash(amount=-3.7, type="withholding", description="ZQY US TAX", flex_transaction_id="990002",
                           raw_extra={"transactionID": "990002"})]
    assert accounts.upsert_account_transactions(CFG, rows) == (2, 0)
    assert _cash_rows(db) == [
        (-3.7, "withholding", "USD", "ZQY US TAX", "990002", 123456, "2026-09-21", "990002"),
        (12.34, "dividend", "USD", "ZQY CASH DIVIDEND", "990001", 123456, "2026-09-21", "990001"),
    ]


def test_cash_rerun_updates_instead_of_duplicating(db) -> None:
    assert accounts.upsert_account_transactions(CFG, [_cash()]) == (1, 0)
    assert accounts.upsert_account_transactions(CFG, [_cash(description="ZQY CASH DIVIDEND USD 0.10")]) == (1, 0)
    rows = _cash_rows(db)
    assert len(rows) == 1 and rows[0][3] == "ZQY CASH DIVIDEND USD 0.10"


def test_cash_rows_without_account_ts_or_report_date_are_not_stored(db) -> None:
    assert accounts.upsert_account_transactions(
        CFG, [_cash(account_id=""), _cash(ts=None), _cash(report_date=""), _cash(amount=1.0)]
    ) == (1, 3)
    assert [r[0] for r in _cash_rows(db)] == [1.0]


def test_a_failed_cash_write_lands_nothing_and_reports_nothing(db) -> None:
    """One bad row (an impossible report date) fails the batch: the good row before it does not land.

    The call must not report rows written. Today it returns 0; TD-91 makes it raise -- both pass here.
    """
    rows = [_cash(), _cash(amount=5.0, report_date="20261345")]
    try:
        result = accounts.upsert_account_transactions(CFG, rows)
    except Exception:  # noqa: BLE001 -- the TD-91 contract
        result = None
    assert not result
    # The writer closes its connection without committing; here that is the savepoint's rollback.
    db.rollback()
    assert _cash_rows(db) == []
