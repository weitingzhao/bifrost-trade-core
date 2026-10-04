"""TD-80 C2 (core 0.47.0): the Write* twins of the POST / PUT writers the facade still carried.

``create_instance_strict``, ``create_position_category_strict``, ``set_position_category_tag_strict``,
``set_market_streams_symbol_order_strict`` and ``set_instrument_class_strict`` raise what the
bool / ``(id, error)`` writers folded into one ``False``. Scripted fake connections
(``write_fakes``); accounts, symbols and ids are made up.
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg2
import pytest

from bifrost_core.monitor.reader import strategy_instance
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteConflict, WriteFailed, WriteInvalid
from bifrost_core.portfolio.reader import instrument_class
from bifrost_core.portfolio.reader import position_categories
from write_fakes import FakeConn, Reply

ACCOUNT = "U0000001"
OPENED = 1_788_000_000
DB_DOWN = psycopg2.OperationalError("server closed the connection unexpectedly")


# --- POST /trades -----------------------------------------------------------------------

_TRADE = {"trade_id": 41, "strategy_opportunity_id": 7, "account_id": ACCOUNT, "label": "ZZQ put"}


def test_create_trade_inserts_and_answers_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_instance, "get_instance_by_id", lambda conn, tid: {**_TRADE, "trade_id": tid})
    conn = FakeConn(
        [
            ("SELECT 1 FROM strategy_opportunity", Reply(one=(1,))),
            ("INSERT INTO trade", Reply(one=(41,))),
        ]
    )
    row = strategy_instance.create_instance_strict(conn, 7, f" {ACCOUNT} ", OPENED, label=" ZZQ put ")
    assert row["trade_id"] == 41
    _, params = conn.statement("INSERT INTO trade")
    assert params == (7, ACCOUNT, datetime.fromtimestamp(OPENED, tz=timezone.utc), "ZZQ put")
    assert conn.commits == 1 and conn.rollbacks == 0


def test_create_trade_with_no_such_opportunity_is_invalid_and_writes_nothing() -> None:
    conn = FakeConn([("SELECT 1 FROM strategy_opportunity", Reply(one=None))])
    with pytest.raises(WriteInvalid, match="No strategy opportunity 7"):
        strategy_instance.create_instance_strict(conn, 7, ACCOUNT, OPENED)
    assert not conn.ran("INSERT") and conn.commits == 0 and conn.rollbacks == 1


def test_create_trade_input_rules() -> None:
    conn = FakeConn()
    with pytest.raises(WriteInvalid, match="account_id is required"):
        strategy_instance.create_instance_strict(conn, 7, "  ", OPENED)
    with pytest.raises(WriteInvalid, match="strategy_opportunity_id must be 1 or more"):
        strategy_instance.create_instance_strict(conn, 0, ACCOUNT, OPENED)
    with pytest.raises(WriteInvalid, match="opened_at is required"):
        strategy_instance.create_instance_strict(conn, 7, ACCOUNT, None)
    with pytest.raises(WriteInvalid, match="label is blank; send null"):
        strategy_instance.create_instance_strict(conn, 7, ACCOUNT, OPENED, label=" ")
    assert conn.executed == []  # refused before any statement


def test_create_trade_db_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(WriteFailed) as down:
        strategy_instance.create_instance_strict(None, 7, ACCOUNT, OPENED)
    assert down.value.unavailable
    conn = FakeConn([("SELECT 1 FROM strategy_opportunity", Reply(raises=DB_DOWN))])
    with pytest.raises(WriteFailed) as failed:
        strategy_instance.create_instance_strict(conn, 7, ACCOUNT, OPENED)
    assert not failed.value.unavailable and conn.rollbacks == 1
    # A row that cannot be read back is not answered as written.
    monkeypatch.setattr(strategy_instance, "get_instance_by_id", lambda conn, tid: None)
    conn = FakeConn([("SELECT 1 FROM strategy_opportunity", Reply(one=(1,))), ("INSERT INTO trade", Reply(one=(41,)))])
    with pytest.raises(WriteFailed, match="could not be read back"):
        strategy_instance.create_instance_strict(conn, 7, ACCOUNT, OPENED)
    assert conn.commits == 0 and conn.rollbacks == 1


def test_create_trade_from_a_status_config_opens_and_closes_its_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn([("SELECT 1 FROM strategy_opportunity", Reply(one=(1,))), ("INSERT INTO trade", Reply(one=(41,)))])
    monkeypatch.setattr(ws, "open_conn", lambda cfg, golden=False: conn)
    monkeypatch.setattr(strategy_instance, "get_instance_by_id", lambda c, tid: {**_TRADE, "trade_id": tid})
    assert strategy_instance.create_instance_strict({"sink": "postgres"}, 7, ACCOUNT, OPENED)["trade_id"] == 41
    assert conn.closed and conn.commits == 1
    with pytest.raises(WriteFailed) as not_pg:
        strategy_instance.create_instance_strict({"sink": "redis"}, 7, ACCOUNT, OPENED)
    assert not_pg.value.unavailable


# --- POST /position-categories ------------------------------------------------------------

_CATEGORY = {"id": 9, "name": "Income", "description": None, "sort_order": 2, "created_at": None, "updated_at": None}


def test_create_category_answers_the_row() -> None:
    conn = FakeConn([("INSERT INTO preference_position_categories", Reply(one=_CATEGORY))])
    assert position_categories.create_position_category_strict(conn, " Income ", sort_order=2) == _CATEGORY
    sql, params = conn.statement("INSERT INTO preference_position_categories")
    assert "RETURNING id, name, description, sort_order" in sql
    assert params == ["Income", None, 2]
    assert conn.commits == 1


def test_create_category_refusals_write_nothing() -> None:
    for name in ("Uncategorized", " uncategorized "):
        with pytest.raises(WriteInvalid, match="reserved"):
            position_categories.create_position_category_strict(FakeConn(), name)
    with pytest.raises(WriteInvalid, match="name is required"):
        position_categories.create_position_category_strict(FakeConn(), "  ")
    with pytest.raises(WriteInvalid, match="description is blank"):
        position_categories.create_position_category_strict(FakeConn(), "Income", description=" ")
    with pytest.raises(WriteInvalid, match="sort_order must be a whole number"):
        position_categories.create_position_category_strict(FakeConn(), "Income", sort_order=1.5)
    conn = FakeConn([("SELECT 1 FROM preference_position_categories WHERE name", Reply(one=(1,)))])
    with pytest.raises(WriteConflict, match="named 'Income' already exists"):
        position_categories.create_position_category_strict(conn, "Income")
    assert not conn.ran("INSERT") and conn.commits == 0


def test_create_category_race_on_the_unique_name_is_a_conflict() -> None:
    conn = FakeConn([("INSERT INTO preference_position_categories", Reply(raises=psycopg2.errors.UniqueViolation()))])
    with pytest.raises(WriteConflict):
        position_categories.create_position_category_strict(conn, "Income")
    assert conn.rollbacks == 1


# --- PUT /position-categories/tag -------------------------------------------------------------


def test_tag_upserts_after_checking_the_category() -> None:
    conn = FakeConn([("SELECT 1 FROM preference_position_categories WHERE id", Reply(one=(1,)))])
    out = position_categories.set_position_category_tag_strict(conn, ACCOUNT, "ZZZQ|STK|||", 9)
    assert out == {"account_id": ACCOUNT, "contract_key": "ZZZQ|STK|||", "category_id": 9, "cleared": False}
    _, params = conn.statement("INSERT INTO preference_position_category_tags")
    assert params == (ACCOUNT, "ZZZQ|STK|||", 9)
    assert conn.commits == 1


def test_tag_with_no_such_category_is_invalid_and_writes_nothing() -> None:
    conn = FakeConn([("SELECT 1 FROM preference_position_categories WHERE id", Reply(one=None))])
    with pytest.raises(WriteInvalid, match="No position category 9"):
        position_categories.set_position_category_tag_strict(conn, ACCOUNT, "ZZZQ|STK|||", 9)
    assert not conn.ran("INSERT") and conn.commits == 0


def test_clearing_a_tag_says_whether_there_was_one() -> None:
    conn = FakeConn([("DELETE FROM preference_position_category_tags", Reply(rowcount=1))])
    assert position_categories.set_position_category_tag_strict(conn, ACCOUNT, "ZZZQ|STK|||", None)["cleared"] is True
    conn = FakeConn([("DELETE FROM preference_position_category_tags", Reply(rowcount=0))])
    out = position_categories.set_position_category_tag_strict(conn, ACCOUNT, "ZZZQ|STK|||", None)
    assert out["cleared"] is False and out["category_id"] is None and conn.commits == 1


def test_tag_input_rules() -> None:
    with pytest.raises(WriteInvalid, match="account_id is required"):
        position_categories.set_position_category_tag_strict(FakeConn(), " ", "ZZZQ|STK|||", 9)
    with pytest.raises(WriteInvalid, match="contract_key is required"):
        position_categories.set_position_category_tag_strict(FakeConn(), ACCOUNT, "", 9)
    with pytest.raises(WriteInvalid, match="category_id must be a whole number"):
        position_categories.set_position_category_tag_strict(FakeConn(), ACCOUNT, "ZZZQ|STK|||", True)


# --- PUT /position-categories/symbol-order --------------------------------------------------------


def test_symbol_order_replaces_the_category_order_in_one_transaction() -> None:
    conn = FakeConn()
    out = position_categories.set_market_streams_symbol_order_strict(conn, " Income ", ["ZZZQ", " ZZZR "])
    assert out == {"category_name": "Income", "symbols": ["ZZZQ", "ZZZR"]}
    inserts = [p for sql, p in conn.executed if "INSERT INTO preference_market_streams_symbol_order" in sql]
    assert inserts == [("Income", "ZZZQ", 0), ("Income", "ZZZR", 1)]
    assert conn.executed[0][1] == ("Income",) and conn.commits == 1


def test_symbol_order_empty_list_clears_it() -> None:
    conn = FakeConn()
    assert position_categories.set_market_streams_symbol_order_strict(conn, "Uncategorized", [])["symbols"] == []
    assert conn.ran("DELETE FROM preference_market_streams_symbol_order") and not conn.ran("INSERT")


def test_symbol_order_refusals_write_nothing() -> None:
    conn = FakeConn()
    with pytest.raises(WriteInvalid, match="category_name is required"):
        position_categories.set_market_streams_symbol_order_strict(conn, " ", ["ZZZQ"])
    with pytest.raises(WriteInvalid, match="symbols cannot be null"):
        position_categories.set_market_streams_symbol_order_strict(conn, "Income", None)
    with pytest.raises(WriteInvalid, match=r"symbols\[1\] is required"):
        position_categories.set_market_streams_symbol_order_strict(conn, "Income", ["ZZZQ", " "])
    with pytest.raises(WriteInvalid, match=r"symbols\[0\] must be text"):
        position_categories.set_market_streams_symbol_order_strict(conn, "Income", [7])
    with pytest.raises(WriteInvalid, match="symbols lists ZZZQ more than once"):
        position_categories.set_market_streams_symbol_order_strict(conn, "Income", ["ZZZQ", "ZZZR", " ZZZQ"])
    assert conn.executed == []


def test_symbol_order_db_failure_rolls_back() -> None:
    conn = FakeConn([("INSERT INTO preference_market_streams_symbol_order", Reply(raises=DB_DOWN))])
    with pytest.raises(WriteFailed):
        position_categories.set_market_streams_symbol_order_strict(conn, "Income", ["ZZZQ"])
    assert conn.rollbacks == 1 and conn.commits == 0


# --- PUT /instrument-classes/{contract_key} ---------------------------------------------------------

_CLASS = {"contract_key": "ZZFI|STK|||", "instrument_class": "fixed_income", "note": None}


def test_put_class_is_a_full_replace_and_answers_the_row() -> None:
    conn = FakeConn([("INSERT INTO preference_instrument_class", Reply(one=_CLASS))])
    assert instrument_class.set_instrument_class_strict(conn, " ZZFI|STK||| ", "Fixed-Income") == _CLASS
    sql, params = conn.statement("INSERT INTO preference_instrument_class")
    assert params == ("ZZFI|STK|||", "fixed_income", None)
    assert "note = EXCLUDED.note" in sql and "COALESCE" not in sql  # no note clears a stored one
    assert conn.commits == 1


def test_put_class_refusals_write_nothing() -> None:
    conn = FakeConn()
    with pytest.raises(WriteInvalid, match="contract_key is required"):
        instrument_class.set_instrument_class_strict(conn, " ", "stock")
    with pytest.raises(WriteInvalid, match="instrument_class must be one of stock, fixed_income, cash_like"):
        instrument_class.set_instrument_class_strict(conn, "ZZFI|STK|||", "bond")
    with pytest.raises(WriteInvalid, match="instrument_class is required"):
        instrument_class.set_instrument_class_strict(conn, "ZZFI|STK|||", None)
    with pytest.raises(WriteInvalid, match="note is blank"):
        instrument_class.set_instrument_class_strict(conn, "ZZFI|STK|||", "stock", note="  ")
    assert conn.executed == []
    with pytest.raises(WriteFailed) as down:
        instrument_class.set_instrument_class_strict(None, "ZZFI|STK|||", "stock")
    assert down.value.unavailable
