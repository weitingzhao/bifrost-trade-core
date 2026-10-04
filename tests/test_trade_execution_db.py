"""TD-09 against real Postgres: the env attribution table (``trade_execution`` since naming R3,
core 0.45.0) and the env views over it.

Marked `db` (``make test-db``). Golden Source's ``raw_broker`` tables live in the same
database; ``brokerage`` holds pass-through views of them standing in for the FDW tables,
and the env views are built over those exactly as ``setup_fdw_foreign_tables`` builds them.
Everything runs in the fixture's transaction and is rolled back. Symbols, exec ids and
accounts are made up. (The one-off TD-09 migration and its tests left with core 0.45.0: it
ran on dev, stg and prod on 2026-10-03 and named the tables R3 renames.)
"""

from __future__ import annotations

from typing import Any

import psycopg2
import pytest

from bifrost_core.monitor.reader import strategy_instance
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteConflict, WriteInvalid
from bifrost_core.persistence.postgres.brokerage_ddl import _create_brokerage_views, ensure_brokerage_schema
from bifrost_core.portfolio.reader import accounts

pytestmark = pytest.mark.db

ACCT = "U0000001"
OTHER = "U0000002"
CFG = {"sink": "postgres"}
RAW = ("executions_raw_flex", "executions_raw_tws", "executions_raw_journal")


class _Savepointed:
    """The fixture's connection with commit / rollback mapped onto one savepoint."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._run("SAVEPOINT td09")

    def _run(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def cursor(self, **kw: Any) -> Any:
        return self._conn.cursor(**kw)

    def commit(self) -> None:
        self._run("RELEASE SAVEPOINT td09")
        self._run("SAVEPOINT td09")

    def rollback(self) -> None:
        self._run("ROLLBACK TO SAVEPOINT td09")

    def close(self) -> None:
        return None


@pytest.fixture
def db(pg_conn, monkeypatch: pytest.MonkeyPatch) -> _Savepointed:
    conn = _Savepointed(pg_conn)
    ensure_brokerage_schema(conn, log=lambda m: None)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS brokerage CASCADE")
        cur.execute("CREATE SCHEMA brokerage")
        for t in RAW:
            cur.execute(f"CREATE VIEW brokerage.{t} AS SELECT * FROM raw_broker.{t}")
        _create_brokerage_views(cur, "brokerage", env=True)
    conn.commit()
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: conn)
    return conn


def _one(db: _Savepointed, sql: str, params: Any = None) -> Any:
    with db.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone() if cur.description is not None else None
    db.commit()
    return row


def _all(db: _Savepointed, sql: str, params: Any = None) -> list:
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _opportunity(db: _Savepointed, name: str) -> int:
    tpl = _one(db, "INSERT INTO strategy_template (template_code, display_name) VALUES (%s, 'TD09') "
                   "RETURNING strategy_template_id", (f"td09_{name}",))[0]
    struct = _one(db, "INSERT INTO strategy_structure (name, strategy_template_id) VALUES (%s, %s) "
                      "RETURNING strategy_structure_id", (f"TD09 {name}", tpl))[0]
    return _one(db, "INSERT INTO strategy_opportunity (name, strategy_structure_id, scope_type) "
                    "VALUES (%s, %s, 'explicit_symbols') RETURNING strategy_opportunity_id", (f"TD09 {name}", struct))[0]


def _instance(db: _Savepointed, opp: int, account: str = ACCT, iid: int | None = None) -> int:
    if iid is None:
        return _one(db, "INSERT INTO trade (strategy_opportunity_id, account_id, opened_at) "
                        "VALUES (%s, %s, now()) RETURNING trade_id", (opp, account))[0]
    return _one(db, "INSERT INTO trade (trade_id, strategy_opportunity_id, account_id, opened_at) "
                    "VALUES (%s, %s, %s, now()) RETURNING trade_id", (iid, opp, account))[0]


def _fill(db: _Savepointed, table: str, exec_id: str, account: str = ACCT, iid: int | None = None,
          opp: int | None = None, qty: float = 1.0, side: str = "BUY") -> int:
    pk = f"{table}_id"
    source = {"executions_raw_flex": "flex_trades", "executions_raw_tws": "tws_client",
              "executions_raw_journal": "journal_closed"}[table]
    return _one(db, f"INSERT INTO raw_broker.{table} (exec_id, account_id, symbol, sec_type, side, quantity, source, "
                    f"strategy_instance_id, strategy_opportunity_id) VALUES (%s, %s, 'TDXV', 'STK', %s, %s, %s, %s, %s) "
                    f"RETURNING {pk}", (exec_id, account, side, qty, source, iid, opp))[0]


def test_the_table_ties_a_fill_to_an_instance_of_its_own_account(db) -> None:
    opp = _opportunity(db, "a")
    inst = _instance(db, opp)
    _one(db, "INSERT INTO trade_execution (account_id, exec_id, trade_id) VALUES (%s, 'td09.a', %s)",
         (ACCT, inst))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _one(db, "INSERT INTO trade_execution (account_id, exec_id, trade_id) VALUES (%s, 'td09.b', %s)",
             (OTHER, inst))
    db.rollback()
    with pytest.raises(psycopg2.errors.UniqueViolation):  # one whole-fill row per fill
        other = _instance(db, opp)
        _one(db, "INSERT INTO trade_execution (account_id, exec_id, trade_id) VALUES (%s, 'td09.a', %s)",
             (ACCT, other))
    db.rollback()
    with pytest.raises(psycopg2.errors.CheckViolation):
        _one(db, "INSERT INTO trade_execution (account_id, exec_id, trade_id, split_quantity) "
                 "VALUES (%s, 'td09.c', %s, 0)", (ACCT, inst))
    db.rollback()
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):  # ON DELETE RESTRICT
        _one(db, "DELETE FROM trade WHERE trade_id = %s", (inst,))


def test_env_views_read_this_env_and_ignore_golden_source_columns(db) -> None:
    opp, stale = _opportunity(db, "v"), _opportunity(db, "stale")
    inst = _instance(db, opp)
    flex = _fill(db, "executions_raw_flex", "td09.v1", iid=999, opp=stale)  # Golden Source says 999 / stale
    tws = _fill(db, "executions_raw_tws", "td09.v1", iid=999, opp=stale)  # the Flex twin's TWS row
    _fill(db, "executions_raw_tws", "td09.v2")  # TWS only, unattributed
    _one(db, "INSERT INTO trade_execution (account_id, exec_id, trade_id) VALUES (%s, 'td09.v1', %s)",
         (ACCT, inst))
    rows = _all(db, "SELECT account_executions_id, trade_id, strategy_opportunity_id "
                    "FROM brokerage.executions WHERE exec_id LIKE 'td09.v%%' ORDER BY exec_id")
    assert [r[1:] for r in rows] == [(inst, opp), (None, None)]
    assert rows[0][0] == flex  # the Flex row shadows its TWS twin, as before
    assert _all(db, "SELECT trade_id FROM brokerage.executions_final WHERE exec_id = 'td09.v1'") == [(inst,)]
    tws_rows = _all(db, "SELECT account_executions_id, trade_id FROM brokerage.executions_tws "
                        "WHERE exec_id LIKE 'td09.v%%' ORDER BY exec_id")
    assert tws_rows[0] == (-tws, inst)  # the twin carries the same attribution
    assert tws_rows[1][1] is None
    cols = [r[0] for r in _all(db, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'brokerage' "
                                   "AND table_name = 'executions' ORDER BY ordinal_position")]
    gs = [r[0] for r in _all(db, "SELECT column_name FROM information_schema.columns WHERE table_schema = 'raw_broker' "
                                 "AND table_name = 'executions' ORDER BY ordinal_position")]
    # Golden Source's names, in its order, but IB's TradeID / RelatedTradeID as ib_* and the
    # attribution as trade_id (naming R3; its one-version alias went in R4)
    renamed = {"trade_id": ["ib_trade_id"], "related_trade_id": ["ib_related_trade_id"],
               "strategy_instance_id": ["trade_id"]}
    assert cols == [n for c in gs for n in renamed.get(c, [c])]


def test_split_rows_reach_every_representation_of_the_fill(db) -> None:
    opp = _opportunity(db, "s")
    a, b = _instance(db, opp), _instance(db, opp)
    flex = _fill(db, "executions_raw_flex", "td09.s1", qty=3.0)
    tws = _fill(db, "executions_raw_tws", "td09.s1", qty=3.0)
    out = accounts.patch_execution(CFG, -tws, {"fill_splits": [
        {"trade_id": a, "quantity": 1}, {"trade_id": b, "quantity": 2}]})
    assert [x["trade_id"] for x in out["fill_splits"]] == [a, b]
    got = _all(db, "SELECT account_executions_id, trade_id, quantity FROM brokerage.trade_fill_splits "
                   "WHERE exec_id = 'td09.s1' ORDER BY 1, 2")
    assert got == [(-tws, a, 1.0), (-tws, b, 2.0), (flex, a, 1.0), (flex, b, 2.0)]
    # naming R4: R3's compatibility view is gone
    assert _all(db, "SELECT to_regclass('brokerage.instance_allocations')") == [(None,)]
    assert _all(db, "SELECT trade_id FROM brokerage.executions WHERE exec_id = 'td09.s1'") == [(None,)]
    # a whole-fill instance on a split fill is refused until the splits are cleared
    with pytest.raises(WriteConflict, match="split across 2 trades"):
        accounts.patch_execution(CFG, flex, {"trade_id": a})
    out = accounts.patch_execution(CFG, flex, {"fill_splits": [], "trade_id": a})
    assert out["trade_id"] == a and out["fill_splits"] == []
    assert _all(db, "SELECT count(*) FROM trade_execution WHERE exec_id = 'td09.s1'") == [(1,)]


def test_writers_never_touch_golden_source_columns(db) -> None:
    opp = _opportunity(db, "w")
    inst = _instance(db, opp)
    flex = _fill(db, "executions_raw_flex", "td09.w1")
    accounts.patch_execution(CFG, flex, {"strategy_opportunity_id": opp, "trade_id": inst})
    with pytest.raises(WriteInvalid, match="Send trade_id"):
        accounts.patch_execution(CFG, flex, {"strategy_opportunity_id": opp})
    assert accounts.update_one_execution(CFG, flex, {"price": 2.5, "trade_id": inst})
    new_id = accounts.insert_one_execution(CFG, {"account_id": ACCT, "time": 1_700_000_000, "symbol": "TDXV",
                                                 "side": "BUY", "quantity": 1, "price": 1, "source": "journal_closed",
                                                 "trade_id": inst})
    assert new_id is not None
    assert accounts.insert_one_execution(CFG, {"account_id": ACCT, "time": 1_700_000_000, "symbol": "TDXV", "side": "BUY",
                                               "quantity": 1, "price": 1, "strategy_opportunity_id": opp}) is None
    for t in RAW:
        assert _all(db, f"SELECT count(*) FROM raw_broker.{t} WHERE strategy_instance_id IS NOT NULL "
                        "OR strategy_opportunity_id IS NOT NULL") == [(0,)]
    assert _all(db, "SELECT count(*) FROM brokerage.executions WHERE trade_id = %s", (inst,)) == [(2,)]
    # a fill on another account cannot be moved while attributed
    assert not accounts.update_one_execution(CFG, flex, {"account_id": OTHER})
    # deleting the fill removes its attribution
    assert accounts.delete_execution_strict(CFG, new_id)["deleted"] == "hard"
    assert _all(db, "SELECT count(*) FROM trade_execution WHERE trade_id = %s", (inst,)) == [(1,)]


def test_instance_delete_counts_this_env(db) -> None:
    opp = _opportunity(db, "d")
    inst = _instance(db, opp)
    flex = _fill(db, "executions_raw_flex", "td09.d1")
    _fill(db, "executions_raw_tws", "td09.d1")
    accounts.patch_execution(CFG, flex, {"trade_id": inst})
    assert strategy_instance.count_attributed_executions(CFG, inst) == 1  # the twins are one fill
    with pytest.raises(WriteConflict, match="^1 fill is attributed to this trade.$"):
        strategy_instance.delete_instance_strict(CFG, inst)
    accounts.patch_execution(CFG, flex, {"trade_id": None})
    assert strategy_instance.delete_instance_strict(CFG, inst)["deleted"] == "hard"
