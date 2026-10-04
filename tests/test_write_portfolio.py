"""TD-15 portfolio, execution and watchlist writers, and the watchlist re-add fix.

Scripted fake connections (``write_fakes``); accounts, symbols and ids are made up.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import pytest

from bifrost_core.monitor.reader import watchlist
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import (
    WriteConflict,
    WriteFailed,
    WriteInvalid,
    WriteNotFound,
)
from bifrost_core.portfolio.reader import accounts
from bifrost_core.portfolio.reader import instrument_class
from bifrost_core.portfolio.reader import option_stock_link
from bifrost_core.portfolio.reader import position_categories
from write_fakes import FakeConn, Reply

CFG = {"sink": "postgres"}
DB_DOWN = psycopg2.OperationalError("server closed the connection unexpectedly")
ACCOUNT = "U0000001"


# --- position categories --------------------------------------------------------------

_CATEGORY = {"id": 3, "name": "Income", "description": None, "sort_order": 2, "created_at": None, "updated_at": None}


def test_position_category_patch_returns_the_row() -> None:
    conn = FakeConn(
        [
            ("SELECT name FROM preference_position_categories", Reply(one={"name": "Yield"})),
            ("UPDATE preference_position_categories", Reply(one=_CATEGORY)),
        ]
    )
    assert position_categories.patch_position_category(conn, 3, {"name": " Income ", "description": None}) == _CATEGORY
    sql, params = conn.statement("UPDATE preference_position_categories")
    assert "RETURNING id, name, description, sort_order" in sql
    assert params == ["Income", None, 3]
    # TD-56: the symbol order moves with the name, in the same transaction.
    _, moved = conn.statement("UPDATE preference_market_streams_symbol_order")
    assert moved == ("Income", "Yield")
    assert conn.commits == 1


def test_position_category_patch_without_a_new_name_leaves_the_symbol_order() -> None:
    conn = FakeConn(
        [
            ("SELECT name FROM preference_position_categories", Reply(one={"name": "Yield"})),
            ("UPDATE preference_position_categories", Reply(one=_CATEGORY)),
        ]
    )
    position_categories.patch_position_category(conn, 3, {"sort_order": 1})
    assert not conn.ran("preference_market_streams_symbol_order")


def test_position_category_names_uncategorized_is_reserved_and_a_taken_name_conflicts() -> None:
    for name in ("Uncategorized", " uncategorized "):
        with pytest.raises(WriteInvalid, match="reserved"):
            position_categories.patch_position_category(FakeConn(), 3, {"name": name})
        with pytest.raises(WriteInvalid, match="reserved"):
            position_categories.create_position_category(FakeConn(), name)
    taken = [
        ("SELECT name FROM preference_position_categories", Reply(one={"name": "Yield"})),
        ("SELECT 1 FROM preference_position_categories WHERE name", Reply(one=(1,))),
    ]
    with pytest.raises(WriteConflict, match="named 'Income' already exists"):
        position_categories.patch_position_category(FakeConn(taken), 3, {"name": "Income"})
    conn = FakeConn([("SELECT 1 FROM preference_position_categories WHERE name", Reply(one=(1,)))])
    with pytest.raises(WriteConflict, match="already exists"):
        position_categories.create_position_category(conn, "Income")
    assert not conn.ran("INSERT")


def test_position_category_patch_rules() -> None:
    with pytest.raises(WriteInvalid, match="Nothing to change"):
        position_categories.patch_position_category(FakeConn(), 3, {})
    with pytest.raises(WriteInvalid, match="Unknown position category field: colour"):
        position_categories.patch_position_category(FakeConn(), 3, {"colour": "red"})
    with pytest.raises(WriteInvalid, match="name is required"):
        position_categories.patch_position_category(FakeConn(), 3, {"name": ""})
    with pytest.raises(WriteInvalid, match="send null to clear"):
        position_categories.patch_position_category(FakeConn(), 3, {"description": ""})
    conn = FakeConn([("UPDATE preference_position_categories", Reply(one=None, rowcount=0))])
    with pytest.raises(WriteNotFound, match="No position category 3"):
        position_categories.patch_position_category(conn, 3, {"sort_order": 1})
    conn = FakeConn([("UPDATE", Reply(raises=DB_DOWN))])
    with pytest.raises(WriteFailed):
        position_categories.patch_position_category(conn, 3, {"sort_order": None})


def test_position_category_strict_delete_reports_what_went_with_it() -> None:
    conn = FakeConn(
        [
            ("FOR UPDATE", Reply(one=("Income",))),
            ("FROM preference_position_category_tags", Reply(one=(4,))),
            ("FROM watchlist", Reply(one=(2,))),
            ("DELETE FROM preference_market_streams_symbol_order", Reply(rowcount=5)),
        ]
    )
    assert position_categories.delete_position_category_strict(conn, 3) == {
        "deleted": "hard",
        "id": 3,
        "tags_removed": 4,
        "watchlist_uncategorized": 2,
        "symbol_order_removed": 5,
    }
    assert conn.statement("DELETE FROM preference_market_streams_symbol_order")[1] == ("Income",)
    with pytest.raises(WriteNotFound):
        position_categories.delete_position_category_strict(FakeConn([("FOR UPDATE", Reply(one=None))]), 3)
    with pytest.raises(WriteFailed):
        position_categories.delete_position_category_strict(FakeConn([("FOR UPDATE", Reply(raises=DB_DOWN))]), 3)


# --- instrument class -----------------------------------------------------------------

_CLASS = {"contract_key": "XBIL|STK|||", "instrument_class": "cash_like", "note": None}


def test_instrument_class_patch_can_clear_the_note() -> None:
    conn = FakeConn([("UPDATE preference_instrument_class", Reply(one=_CLASS))])
    assert instrument_class.patch_instrument_class(conn, "XBIL|STK|||", {"note": None, "instrument_class": "Cash-Like"}) == _CLASS
    _, params = conn.statement("UPDATE preference_instrument_class")
    assert params == ["cash_like", None, "XBIL|STK|||"]


def test_instrument_class_patch_and_delete_rules() -> None:
    with pytest.raises(WriteInvalid, match="must be one of"):
        instrument_class.patch_instrument_class(FakeConn(), "XBIL|STK|||", {"instrument_class": "bond"})
    with pytest.raises(WriteInvalid, match="instrument_class is required"):
        instrument_class.patch_instrument_class(FakeConn(), "XBIL|STK|||", {"instrument_class": None})
    with pytest.raises(WriteInvalid, match="contract_key is required"):
        instrument_class.patch_instrument_class(FakeConn(), " ", {"note": None})
    conn = FakeConn([("UPDATE preference_instrument_class", Reply(one=None))])
    with pytest.raises(WriteNotFound, match="no instrument class registered"):
        instrument_class.patch_instrument_class(conn, "XBIL|STK|||", {"note": "T-bills"})
    assert instrument_class.delete_instrument_class_strict(FakeConn(), "XBIL|STK|||") == {
        "deleted": "hard",
        "contract_key": "XBIL|STK|||",
    }
    with pytest.raises(WriteNotFound):
        instrument_class.delete_instrument_class_strict(FakeConn([("DELETE", Reply(rowcount=0))]), "XBIL|STK|||")
    with pytest.raises(WriteFailed):
        instrument_class.delete_instrument_class_strict(FakeConn([("DELETE", Reply(raises=DB_DOWN))]), "XBIL|STK|||")


# --- option / stock links ------------------------------------------------------------


def test_option_stock_link_strict_delete() -> None:
    assert option_stock_link.delete_option_stock_link_strict(FakeConn(), 9, ACCOUNT) == {
        "deleted": "hard",
        "account_execution_option_stock_link_id": 9,
    }
    with pytest.raises(WriteNotFound, match=f"No option/stock link 9 on account {ACCOUNT}"):
        option_stock_link.delete_option_stock_link_strict(FakeConn([("DELETE", Reply(rowcount=0))]), 9, ACCOUNT)
    with pytest.raises(WriteInvalid, match="account_id is required"):
        option_stock_link.delete_option_stock_link_strict(FakeConn(), 9, " ")
    with pytest.raises(WriteInvalid):
        option_stock_link.delete_option_stock_link_strict(FakeConn(), "nine", ACCOUNT)
    with pytest.raises(WriteFailed):
        option_stock_link.delete_option_stock_link_strict(FakeConn([("DELETE", Reply(raises=DB_DOWN))]), 9, ACCOUNT)


# --- executions: Golden Source raw row + per-env splits ----------------------------------


@pytest.fixture
def two_dbs(monkeypatch: pytest.MonkeyPatch):
    def install(env: FakeConn, golden_conn: Any) -> None:
        def route(params, golden=False):
            if not golden:
                return env
            if isinstance(golden_conn, BaseException):
                raise golden_conn
            return golden_conn

        monkeypatch.setattr(ws, "connect", route)

    return install


EXEC = "0000e1.01"
_RAW = "SELECT account_id, quantity, side, source, exec_id FROM raw_broker.executions_raw_flex"


def _golden(**over: Any) -> FakeConn:
    """Golden Source: the raw row (lock and plain reads) and the twin check."""
    return FakeConn(
        [
            (_RAW, over.get("lock", Reply(one=(ACCOUNT, 2.0, "BUY", "flex_trades", EXEC)))),
            ("SELECT 1 FROM raw_broker.executions_raw_", over.get("twin", Reply(one=None))),
        ]
    )


def _env(**over: Any) -> FakeConn:
    """The env database: instance check, split count, attribution read-back and writes."""
    return FakeConn(
        [
            ("SELECT account_id, strategy_opportunity_id FROM trade", over.get("instance", Reply(one=(ACCOUNT, 5)))),
            ("SELECT count(*) FROM trade_execution", over.get("splits", Reply(one=(0,)))),
            ("FROM trade_execution sie", over.get("read", Reply(all=[(41, None, "L", 5)]))),
            ("INSERT INTO trade_execution", over.get("insert", Reply())),
        ]
    )


def test_patch_execution_sets_direct_attribution_and_returns_it(two_dbs) -> None:
    env, golden = _env(), _golden()
    two_dbs(env, golden)
    out = accounts.patch_execution(CFG, 77, {"strategy_opportunity_id": 5, "strategy_instance_id": 41})
    assert out == {
        "account_executions_id": 77,
        "account_id": ACCOUNT,
        "strategy_opportunity_id": 5,
        "strategy_instance_id": 41,
        "instance_allocations": [],
        # naming R1 (core 0.42.0): the trade names beside them
        "trade_id": 41,
        "fill_splits": [],
    }
    # TD-09: this env's table, keyed by the fill; Golden Source is only read.
    sql, params = env.statement("INSERT INTO trade_execution")
    assert "ON CONFLICT (account_id, exec_id) WHERE split_quantity IS NULL" in sql
    assert params == (ACCOUNT, EXEC, 41)
    assert not golden.ran("UPDATE raw_broker")
    assert golden.commits == 1 and env.commits == 1 and golden.closed and env.closed


def test_patch_execution_null_clears_the_whole_fill_row(two_dbs) -> None:
    env, golden = _env(read=Reply(all=[])), _golden()
    two_dbs(env, golden)
    out = accounts.patch_execution(CFG, 77, {"strategy_instance_id": None})
    sql, params = env.statement("DELETE FROM trade_execution")
    assert "split_quantity IS NULL" in sql and params == (ACCOUNT, EXEC)
    assert out["strategy_instance_id"] is None and out["strategy_opportunity_id"] is None
    # strategy_opportunity_id: null alone changes nothing.
    env, golden = _env(), _golden()
    two_dbs(env, golden)
    accounts.patch_execution(CFG, 77, {"strategy_opportunity_id": None})
    assert not env.ran("DELETE") and not env.ran("INSERT")


def test_patch_execution_refusals(two_dbs) -> None:
    with pytest.raises(WriteInvalid, match="Nothing to change"):
        accounts.patch_execution(CFG, 77, {})
    with pytest.raises(WriteInvalid, match="Unknown execution field: price"):
        accounts.patch_execution(CFG, 77, {"price": 1.0})
    with pytest.raises(WriteInvalid, match="one way or the other"):
        accounts.patch_execution(
            CFG, 77, {"strategy_instance_id": 41, "instance_allocations": [{"strategy_instance_id": 41, "allocated_quantity": 1}]}
        )
    with pytest.raises(WriteInvalid, match=r"send \[\]"):
        accounts.patch_execution(CFG, 77, {"instance_allocations": None})
    # An opportunity is reached through a trade (TD-09).
    with pytest.raises(WriteInvalid, match="Send strategy_instance_id"):
        accounts.patch_execution(CFG, 77, {"strategy_opportunity_id": 5})
    with pytest.raises(WriteInvalid, match="Send strategy_instance_id"):
        accounts.patch_execution(CFG, 77, {"strategy_opportunity_id": 5, "strategy_instance_id": None})

    two_dbs(_env(), _golden())
    with pytest.raises(WriteInvalid, match="under opportunity 5, not 6"):
        accounts.patch_execution(CFG, 77, {"strategy_opportunity_id": 6, "strategy_instance_id": 41})

    two_dbs(_env(), _golden(lock=Reply(one=None)))
    with pytest.raises(WriteNotFound, match="No execution 77"):
        accounts.patch_execution(CFG, 77, {"strategy_instance_id": 41})

    two_dbs(_env(), _golden(lock=Reply(one=(ACCOUNT, 2.0, "BUY", "flex_trades", None))))
    with pytest.raises(WriteInvalid, match="has no exec_id"):
        accounts.patch_execution(CFG, 77, {"strategy_instance_id": 41})

    two_dbs(_env(instance=Reply(one=("U0000002", 5))), _golden())
    with pytest.raises(WriteInvalid, match="belongs to account U0000002"):
        accounts.patch_execution(CFG, 77, {"strategy_instance_id": 41})

    two_dbs(_env(instance=Reply(one=None)), _golden())
    with pytest.raises(WriteInvalid, match="No trade 41"):
        accounts.patch_execution(CFG, 77, {"strategy_instance_id": 41})

    env, golden = _env(splits=Reply(one=(2,))), _golden()
    two_dbs(env, golden)
    with pytest.raises(WriteConflict, match="split across 2 trades"):
        accounts.patch_execution(CFG, 77, {"strategy_instance_id": 41})
    assert not env.ran("INSERT") and golden.rollbacks == 1 and env.rollbacks == 1

    two_dbs(_env(insert=Reply(raises=DB_DOWN)), _golden())
    with pytest.raises(WriteFailed):
        accounts.patch_execution(CFG, 77, {"strategy_instance_id": 41})

    two_dbs(_env(), psycopg2.OperationalError("could not connect"))
    with pytest.raises(WriteFailed, match="Golden Source is unreachable"):
        accounts.patch_execution(CFG, 77, {"strategy_instance_id": 41})

    with pytest.raises(WriteFailed, match="status config is needed"):
        accounts.patch_execution(FakeConn(), 77, {"strategy_instance_id": 41})


def test_patch_execution_replacing_splits_with_a_direct_id(two_dbs) -> None:
    env, golden = _env(splits=Reply(one=(2,))), _golden()
    two_dbs(env, golden)
    accounts.patch_execution(CFG, 77, {"instance_allocations": [], "strategy_instance_id": 41})
    sql, params = env.statement("DELETE FROM trade_execution")
    assert "split_quantity IS NOT NULL" in sql and params == (ACCOUNT, EXEC)
    assert env.statement("INSERT INTO trade_execution")[1] == (ACCOUNT, EXEC, 41)
    # the splits go first: the whole-fill row may name an instance a split named
    ran = [sql for sql, _ in env.executed]
    assert ran.index(sql) < next(i for i, x in enumerate(ran) if x.startswith("INSERT"))


def test_patch_execution_splits_replace_the_whole_fill_row(two_dbs) -> None:
    env, golden = _env(read=Reply(all=[(41, 1.5, "A", 5), (42, 0.5, None, 6)])), _golden()
    two_dbs(env, golden)
    out = accounts.patch_execution(
        CFG,
        77,
        {"instance_allocations": [{"strategy_instance_id": 41, "allocated_quantity": 1.5}, {"strategy_instance_id": 42, "allocated_quantity": 0.5}]},
    )
    deletes = [sql for sql, _ in env.executed if sql.startswith("DELETE FROM trade_execution")]
    assert any("IS NOT NULL" in d for d in deletes) and any("IS NULL" in d and "NOT NULL" not in d for d in deletes)
    inserts = [p for sql, p in env.executed if sql.startswith("INSERT INTO trade_execution")]
    assert inserts == [(ACCOUNT, EXEC, 41, 1.5), (ACCOUNT, EXEC, 42, 0.5)]
    assert out["strategy_instance_id"] is None
    assert out["instance_allocations"] == [
        {"strategy_instance_id": 41, "allocated_quantity": 1.5, "strategy_opportunity_id": 5, "strategy_instance_label": "A"},
        {"strategy_instance_id": 42, "allocated_quantity": 0.5, "strategy_opportunity_id": 6},
    ]


def test_patch_execution_bad_splits_are_invalid(two_dbs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accounts, "_apply_instance_allocations_on_cursor", lambda *a, **k: False)
    two_dbs(_env(), _golden())
    with pytest.raises(WriteInvalid, match="adding up to the fill's quantity"):
        accounts.patch_execution(CFG, 77, {"instance_allocations": [{"strategy_instance_id": 41, "allocated_quantity": 3}]})


def test_delete_execution_strict(two_dbs) -> None:
    golden = _golden()
    env = FakeConn(
        [
            ("FROM account_execution_option_stock_link", Reply(one=(0,))),
            ("DELETE FROM trade_execution", Reply(all=[(True,), (True,)])),
        ]
    )
    two_dbs(env, golden)
    assert accounts.delete_execution_strict(CFG, 77) == {
        "deleted": "hard",
        "account_executions_id": 77,
        "allocations_removed": 2,
    }
    sql, params = golden.statement("DELETE FROM raw_broker.commissions")
    assert sql.count("NOT EXISTS") == 3 and params == [EXEC] * 4
    assert env.statement("DELETE FROM trade_execution")[1] == (ACCOUNT, EXEC)
    assert golden.commits == 1 and env.commits == 1


def test_delete_execution_strict_keeps_the_attribution_of_a_surviving_twin(two_dbs) -> None:
    golden = _golden(twin=Reply(one=(1,)))
    env = FakeConn([("FROM account_execution_option_stock_link", Reply(one=(0,)))])
    two_dbs(env, golden)
    assert accounts.delete_execution_strict(CFG, 77)["allocations_removed"] == 0
    assert not env.ran("DELETE FROM trade_execution")


def test_delete_execution_strict_refusals(two_dbs) -> None:
    golden = _golden()
    env = FakeConn([("FROM account_execution_option_stock_link", Reply(one=(1,)))])
    two_dbs(env, golden)
    with pytest.raises(WriteConflict, match="in 1 option/stock link; unlink it first"):
        accounts.delete_execution_strict(CFG, 77)
    assert not golden.ran("DELETE")

    two_dbs(FakeConn(), _golden(lock=Reply(one=None)))
    with pytest.raises(WriteNotFound, match="No execution 77"):
        accounts.delete_execution_strict(CFG, 77)

    two_dbs(FakeConn(), _golden(lock=Reply(raises=DB_DOWN)))
    with pytest.raises(WriteFailed):
        accounts.delete_execution_strict(CFG, 77)


# --- watchlist ---------------------------------------------------------------------------


def test_watchlist_re_add_keeps_category_and_label() -> None:
    """The Omnibar / Symbol Dock / drop add sends no category_id and no display_label.

    Before 0.33.0 the upsert wrote EXCLUDED.* for both, so re-adding a watched symbol
    moved it out of its list and dropped its label. Now a None keeps what is stored.
    """
    conn = FakeConn()
    assert watchlist.add_watchlist(conn, "ABCD", source="omnibar") is True
    sql, params = conn.statement("INSERT INTO watchlist")
    assert "category_id = COALESCE(%(category_id)s, watchlist.category_id)" in sql
    assert "display_label = COALESCE(%(display_label)s, watchlist.display_label)" in sql
    assert "optionable = COALESCE(%(optionable)s, watchlist.optionable)" in sql
    assert "EXCLUDED.category_id" not in sql and "EXCLUDED.display_label" not in sql
    assert params["contract_key"] == "ABCD|STK|||" and params["category_id"] is None
    assert params["symbol"] == "ABCD" and params["sec_type"] == "STK"
    assert conn.commits == 1


def test_watchlist_add_can_still_move_a_row_out_of_its_list() -> None:
    """The Watchlist page's "None" category sends category_id: null on purpose: the API names it in clear."""
    conn = FakeConn()
    watchlist.add_watchlist(conn, "ABCD|STK|||", category_id=None, clear=("category_id",))
    sql, _ = conn.statement("INSERT INTO watchlist")
    assert "category_id = NULL" in sql
    assert "display_label = COALESCE(%(display_label)s, watchlist.display_label)" in sql
    conn = FakeConn()
    watchlist.add_watchlist(conn, "ABCD|STK|||", category_id=7, clear=("category_id",))
    sql, params = conn.statement("INSERT INTO watchlist")
    assert "category_id = COALESCE(%(category_id)s" in sql and params["category_id"] == 7


def test_watchlist_add_new_row_defaults_source_to_manual() -> None:
    conn = FakeConn()
    watchlist.add_watchlist(conn, "ABCD|STK|||")
    sql, params = conn.statement("INSERT INTO watchlist")
    assert "COALESCE(%(source)s, 'manual')" in sql and params["source"] is None


def test_watchlist_add_failure_rolls_back_and_answers_false() -> None:
    conn = FakeConn([("INSERT INTO watchlist", Reply(raises=DB_DOWN))])
    assert watchlist.add_watchlist(conn, "ABCD") is False
    assert conn.rollbacks == 1


_WATCH_ROW = {"contract_key": "ABCD|STK|||", "symbol": "ABCD", "category_id": 3, "display_label": "Core", "optionable": True}


def test_upsert_watchlist_changes_only_what_was_sent() -> None:
    conn = FakeConn([("FROM watchlist w", Reply(one=_WATCH_ROW))])
    assert watchlist.upsert_watchlist(conn, "ABCD", {"optionable": True}) == _WATCH_ROW
    sql, params = conn.statement("INSERT INTO watchlist")
    assert sql.endswith("ON CONFLICT (contract_key) DO UPDATE SET optionable = EXCLUDED.optionable")
    assert "category_id" not in sql and "display_label" not in sql
    assert params == ["ABCD|STK|||", True, "ABCD", "STK", "manual"]


def test_upsert_watchlist_bare_add_and_explicit_clear() -> None:
    conn = FakeConn([("FROM watchlist w", Reply(one=_WATCH_ROW))])
    watchlist.upsert_watchlist(conn, "ABCD|STK|||", {})
    sql, _ = conn.statement("INSERT INTO watchlist")
    assert sql.endswith("DO UPDATE SET contract_key = EXCLUDED.contract_key")
    conn = FakeConn([("FROM watchlist w", Reply(one=_WATCH_ROW))])
    watchlist.upsert_watchlist(conn, "ABCD|STK|||", {"category_id": None})
    sql, params = conn.statement("INSERT INTO watchlist")
    assert "DO UPDATE SET category_id = EXCLUDED.category_id" in sql and params[1] is None


def test_upsert_watchlist_rules() -> None:
    with pytest.raises(WriteInvalid, match="Unknown watchlist field: colour"):
        watchlist.upsert_watchlist(FakeConn(), "ABCD", {"colour": "red"})
    with pytest.raises(WriteInvalid, match="contract_key is required"):
        watchlist.upsert_watchlist(FakeConn(), " ", {})
    with pytest.raises(WriteInvalid, match="true or false"):
        watchlist.upsert_watchlist(FakeConn(), "ABCD", {"optionable": None})
    conn = FakeConn([("INSERT INTO watchlist", Reply(raises=psycopg2.errors.ForeignKeyViolation("fk")))])
    with pytest.raises(WriteInvalid, match="referenced row does not exist"):
        watchlist.upsert_watchlist(conn, "ABCD", {"category_id": 999})
    conn = FakeConn([("INSERT INTO watchlist", Reply(raises=DB_DOWN))])
    with pytest.raises(WriteFailed):
        watchlist.upsert_watchlist(conn, "ABCD", {})


def test_patch_watchlist_item() -> None:
    conn = FakeConn([("FROM watchlist w", Reply(one=_WATCH_ROW))])
    assert watchlist.patch_watchlist_item(conn, "ABCD|STK|||", {"display_label": None, "category_id": 3}) == _WATCH_ROW
    sql, params = conn.statement("UPDATE watchlist SET")
    assert sql == "UPDATE watchlist SET display_label = %s, category_id = %s WHERE contract_key = %s"
    assert params == [None, 3, "ABCD|STK|||"]
    with pytest.raises(WriteInvalid, match="Nothing to change"):
        watchlist.patch_watchlist_item(FakeConn(), "ABCD|STK|||", {})
    with pytest.raises(WriteNotFound, match="not on the watchlist"):
        watchlist.patch_watchlist_item(FakeConn([("UPDATE watchlist", Reply(rowcount=0))]), "ABCD|STK|||", {"optionable": False})
    with pytest.raises(WriteFailed):
        watchlist.patch_watchlist_item(FakeConn([("UPDATE watchlist", Reply(raises=DB_DOWN))]), "ABCD|STK|||", {"optionable": False})


def test_delete_watchlist_strict() -> None:
    assert watchlist.delete_watchlist_strict(FakeConn(), "ABCD") == {"deleted": "hard", "contract_key": "ABCD|STK|||"}
    with pytest.raises(WriteNotFound, match="not on the watchlist"):
        watchlist.delete_watchlist_strict(FakeConn([("DELETE", Reply(rowcount=0))]), "ABCD|STK|||")
    with pytest.raises(WriteFailed, match="not configured"):
        watchlist.delete_watchlist_strict(None, "ABCD|STK|||")


def test_status_reader_add_watchlist_passes_clear_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from bifrost_core.monitor.reader.common import StatusReader

    seen = {}

    def fake_add(conn, *args, clear=()):
        seen["args"], seen["clear"] = args, tuple(clear)
        return True

    reader = StatusReader({"sink": "postgres"})
    monkeypatch.setattr(reader, "_connect", lambda: True)
    monkeypatch.setattr(watchlist, "add_watchlist", fake_add)
    assert reader.add_watchlist("ABCD", category_id=None, clear=["category_id"]) is True
    assert seen["clear"] == ("category_id",)
    assert seen["args"][6] is None  # source: None keeps the stored one
