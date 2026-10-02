"""Saved searches: validation and the SQL each call sends, against a fake connection."""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

from bifrost_core.monitor.reader import saved_search
from bifrost_core.monitor.reader.saved_search import SavedSearchError

CFG = {"sink": "postgres"}


class _Cur:
    def __init__(self, results: List[Any], rowcount: int = 1) -> None:
        self._results = list(results)
        self.rowcount = rowcount
        self.executed: List[tuple] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        return self._results.pop(0) if self._results else None

    def fetchall(self) -> Any:
        return self._results.pop(0) if self._results else []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self, results: Optional[List[Any]] = None, rowcount: int = 1) -> None:
        self.cur = _Cur(results or [], rowcount)
        self.commits = 0

    def cursor(self, **_: Any) -> _Cur:
        return self.cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch):
    def _make(results: Optional[List[Any]] = None, rowcount: int = 1) -> _Conn:
        fake = _Conn(results, rowcount)
        monkeypatch.setattr(saved_search, "_conn_from_config", lambda _cfg: fake)
        return fake

    return _make


def test_lists_none_where_the_table_is_not_there_yet(conn) -> None:
    fake = conn([{"ok": False}])
    assert saved_search.list_saved_searches(CFG) == []
    assert len(fake.cur.executed) == 1


def test_lists_the_operators_rows_with_their_state(conn) -> None:
    conn([{"ok": True}, [{"preference_saved_search_id": 1, "route": "/trade/plans", "label": "AMD · all", "state_json": '{"q": "sym:AMD"}'}]])
    rows = saved_search.list_saved_searches(CFG)
    assert rows[0]["state_json"] == {"q": "sym:AMD"}


def test_saving_a_label_again_replaces_it(conn) -> None:
    fake = conn([(5,)])
    assert saved_search.create_saved_search(CFG, "/trade/plans", "AMD  ·  all", {"q": "sym:AMD"}) == 5
    sql, params = fake.cur.executed[-1]
    assert "ON CONFLICT (owner, route, label) DO UPDATE" in sql
    assert params[:3] == ("operator", "/trade/plans", "AMD · all")
    assert fake.commits == 1


@pytest.mark.parametrize(
    "route,label,state",
    [("trade/plans", "x", {}), ("/trade/plans", "  ", {}), ("/trade/plans", "x" * 121, {}), ("/trade/plans", "x", [])],
)
def test_refuses_what_the_table_would_not_hold(conn, route: str, label: str, state: Any) -> None:
    conn([])
    with pytest.raises(SavedSearchError):
        saved_search.create_saved_search(CFG, route, label, state)


def test_delete_says_whether_a_row_went(conn) -> None:
    conn([], rowcount=1)
    assert saved_search.delete_saved_search(CFG, 5) is True
    conn([], rowcount=0)
    assert saved_search.delete_saved_search(CFG, 404) is False
