"""A scripted fake psycopg2 connection for the TD-15 writer tests.

Each rule is ``(fragment, response)``: the first rule whose fragment occurs in an
executed statement answers it. A response sets what ``fetchone`` / ``fetchall``
return and the ``rowcount``, or raises. A statement no rule matches fetches None
with rowcount 1. Everything executed is kept in ``conn.executed`` for assertions.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


class Reply:
    def __init__(
        self,
        one: Any = None,
        all: Optional[List[Any]] = None,  # noqa: A002 - mirrors fetchall
        rowcount: int = 1,
        raises: Optional[BaseException] = None,
        once: bool = False,
    ) -> None:
        self.one = one
        self.all = all
        self.rowcount = rowcount
        self.raises = raises
        self.once = once


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._one: Any = None
        self._all: List[Any] = []
        self.rowcount = 1

    def execute(self, sql: str, params: Any = None) -> None:
        text = _norm(sql)
        self._conn.executed.append((text, params))
        for i, (fragment, reply) in enumerate(self._conn.rules):
            if fragment in text:
                if reply.once:
                    self._conn.rules.pop(i)
                if reply.raises is not None:
                    raise reply.raises
                self._one = reply.one
                self._all = list(reply.all or [])
                self.rowcount = reply.rowcount
                return
        self._one = None
        self._all = []
        self.rowcount = 1

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> List[Any]:
        return self._all

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeConn:
    def __init__(self, rules: Optional[List[Tuple[str, Reply]]] = None) -> None:
        self.rules: List[Tuple[str, Reply]] = list(rules or [])
        self.executed: List[Tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, **_: Any) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True

    # --- assertions -------------------------------------------------------------
    def ran(self, fragment: str) -> bool:
        return any(fragment in sql for sql, _ in self.executed)

    def statement(self, fragment: str) -> Tuple[str, Any]:
        for sql, params in self.executed:
            if fragment in sql:
                return sql, params
        raise AssertionError(f"no statement with {fragment!r}; ran: {[s for s, _ in self.executed]}")


def as_dict_params(params: Any) -> Dict[str, Any]:
    assert isinstance(params, dict), params
    return params
