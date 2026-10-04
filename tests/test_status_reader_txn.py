"""TD-48: StatusReader's connection reuse and read-transaction handling had no test.

`_connect` keeps one connection per thread: a live one is rolled back and reused, a
closed or broken one is dropped and replaced, and a new one gets the session limits
(lock 5s, statement 5s, idle-in-transaction 15s). `_end_read_txn` ends the implicit
read transaction so the connection does not sit idle in a transaction between requests.
"""

from __future__ import annotations

import threading
from typing import Any, List

import pytest

from bifrost_core.monitor.reader import common
from bifrost_core.monitor.reader.common import StatusReader


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.statements.append(sql)

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self, n: int, *, fail_rollback: bool = False) -> None:
        self.n = n
        self.closed = 0
        self.fail_rollback = fail_rollback
        self.statements: List[str] = []
        self.rollbacks = 0
        self.commits = 0
        self.close_calls = 0

    def cursor(self, **_: Any) -> _Cur:
        return _Cur(self)

    def rollback(self) -> None:
        if self.fail_rollback:
            raise RuntimeError("server closed the connection")
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.close_calls += 1
        self.closed = 1


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> List[_Conn]:
    conns: List[_Conn] = []

    def fake_connect(**params: Any) -> _Conn:
        conns.append(_Conn(len(conns)))
        return conns[-1]

    monkeypatch.setattr(common.psycopg2, "connect", fake_connect)
    return conns


def _reader() -> StatusReader:
    return StatusReader({"sink": "postgres", "postgres": {"host": "db.invalid"}})


def test_new_connection_gets_the_session_limits(opened: List[_Conn]) -> None:
    r = _reader()
    assert r._connect() is True
    (conn,) = opened
    assert conn.statements == [
        "SET lock_timeout = '5s'",
        "SET statement_timeout = '5s'",
        "SET idle_in_transaction_session_timeout = '15s'",
    ]
    assert conn.commits == 1


def test_live_connection_is_rolled_back_and_reused(opened: List[_Conn]) -> None:
    r = _reader()
    r._connect()
    assert r._connect() is True
    assert len(opened) == 1
    assert opened[0].rollbacks == 1


def test_closed_connection_is_replaced(opened: List[_Conn]) -> None:
    r = _reader()
    r._connect()
    opened[0].closed = 1  # the server dropped it
    assert r._connect() is True
    assert len(opened) == 2 and r._conn is opened[1]
    assert opened[0].close_calls == 1


def test_connection_whose_rollback_fails_is_replaced(opened: List[_Conn]) -> None:
    r = _reader()
    r._connect()
    opened[0].fail_rollback = True
    assert r._connect() is True
    assert len(opened) == 2 and r._conn is opened[1]


def test_connect_failure_is_false_and_leaves_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**params: Any) -> Any:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(common.psycopg2, "connect", refuse)
    r = _reader()
    assert r._connect() is False
    assert r._conn is None


def test_end_read_txn_rolls_back_and_keeps_the_connection(opened: List[_Conn]) -> None:
    r = _reader()
    r._connect()
    r._end_read_txn()
    assert opened[0].rollbacks == 1 and r._conn is opened[0]


def test_end_read_txn_drops_a_closed_or_broken_connection(opened: List[_Conn]) -> None:
    r = _reader()
    r._connect()
    opened[0].closed = 1
    r._end_read_txn()
    assert r._conn is None
    r._connect()
    opened[1].fail_rollback = True
    r._end_read_txn()
    assert r._conn is None
    r._end_read_txn()  # nothing open: no-op


def test_each_thread_has_its_own_connection(opened: List[_Conn]) -> None:
    r = _reader()
    r._connect()
    seen: List[Any] = []

    def other() -> None:
        seen.append(r._conn)  # nothing yet on this thread
        r._connect()
        seen.append(r._conn)

    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert seen[0] is None and seen[1] is opened[1]
    assert r._conn is opened[0]
