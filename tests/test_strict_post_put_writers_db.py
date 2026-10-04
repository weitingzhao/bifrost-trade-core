"""TD-80 C2 writers against real Postgres: the SQL, constraints and read-back the fakes cannot check.

Marked `db`: `make test` skips it, `make test-db` runs it. Same savepointed connection as
``test_write_semantics_db`` (writers' commits and rollbacks become savepoints, all rolled back
at teardown). Names, accounts and ids are made up.
"""

from __future__ import annotations

from typing import Any

import pytest

import test_write_semantics_db as td15
from bifrost_core.monitor.reader import strategy_instance
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteConflict, WriteInvalid
from bifrost_core.persistence.postgres.brokerage_ddl import ensure_brokerage_schema
from bifrost_core.portfolio.reader import instrument_class
from bifrost_core.portfolio.reader import position_categories

pytestmark = pytest.mark.db

ACCOUNT = td15.ACCOUNT
MISSING = 2_000_000_000


@pytest.fixture
def db(pg_conn, monkeypatch: pytest.MonkeyPatch) -> Any:
    conn = td15._Savepointed(pg_conn)
    ensure_brokerage_schema(conn, log=lambda m: None)
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: conn)
    return conn


def _count(db: Any, sql: str, params: Any = None) -> int:
    with db.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()[0])


def test_create_trade_in_postgres(db) -> None:
    ids = td15._seed_rule_chain(db)
    before = _count(db, "SELECT count(*) FROM trade")
    row = strategy_instance.create_instance_strict(td15.CFG, ids["opp"], ACCOUNT, 1_788_000_000, label="ZZQ Oct 40P")
    assert row["trade_id"] > ids["inst"] and row["label"] == "ZZQ Oct 40P"
    assert row["strategy_opportunity_name"] == "TD15 opp" and row["opened_at_epoch"] == 1_788_000_000
    with pytest.raises(WriteInvalid, match=f"No strategy opportunity {MISSING}"):
        strategy_instance.create_instance_strict(td15.CFG, MISSING, ACCOUNT, 1_788_000_000)
    assert _count(db, "SELECT count(*) FROM trade") == before + 1


def test_position_category_post_and_put_in_postgres(db) -> None:
    row = position_categories.create_position_category_strict(td15.CFG, "TDC2 Income", sort_order=3)
    cat = row["id"]
    assert row["name"] == "TDC2 Income" and row["sort_order"] == 3 and row["description"] is None
    with pytest.raises(WriteConflict):
        position_categories.create_position_category_strict(td15.CFG, "TDC2 Income")

    out = position_categories.set_position_category_tag_strict(td15.CFG, ACCOUNT, "TDXQ|STK|||", cat)
    assert out["category_id"] == cat
    with pytest.raises(WriteInvalid, match=f"No position category {MISSING}"):
        position_categories.set_position_category_tag_strict(td15.CFG, ACCOUNT, "TDXQ|STK|||", MISSING)
    tagged = "SELECT category_id FROM preference_position_category_tags WHERE account_id = %s AND contract_key = %s"
    with db.cursor() as cur:
        cur.execute(tagged, (ACCOUNT, "TDXQ|STK|||"))
        assert cur.fetchone()[0] == cat  # the refused retag left the tag alone
    assert position_categories.set_position_category_tag_strict(td15.CFG, ACCOUNT, "TDXQ|STK|||", None)["cleared"] is True
    assert position_categories.set_position_category_tag_strict(td15.CFG, ACCOUNT, "TDXQ|STK|||", None)["cleared"] is False

    order = position_categories.set_market_streams_symbol_order_strict(td15.CFG, "TDC2 Income", ["TDXQ", "TDXR"])
    assert order["symbols"] == ["TDXQ", "TDXR"]
    with pytest.raises(WriteInvalid, match="more than once"):
        position_categories.set_market_streams_symbol_order_strict(td15.CFG, "TDC2 Income", ["TDXR", "TDXR"])
    assert position_categories.get_market_streams_symbol_order(db)["TDC2 Income"] == ["TDXQ", "TDXR"]
    position_categories.set_market_streams_symbol_order_strict(td15.CFG, "TDC2 Income", ["TDXR"])
    assert position_categories.get_market_streams_symbol_order(db)["TDC2 Income"] == ["TDXR"]


def test_instrument_class_put_in_postgres(db) -> None:
    row = instrument_class.set_instrument_class_strict(td15.CFG, "TDXF|STK|||", "fixed_income", note="bond fund")
    assert row["instrument_class"] == "fixed_income" and row["note"] == "bond fund"
    row = instrument_class.set_instrument_class_strict(td15.CFG, "TDXF|STK|||", "cash_like")
    assert row["instrument_class"] == "cash_like" and row["note"] is None  # a full replace
    assert _count(db, "SELECT count(*) FROM preference_instrument_class WHERE contract_key = 'TDXF|STK|||'") == 1
