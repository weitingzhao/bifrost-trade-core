"""TD-46: one connect helper for every reader / writer that opens its own connection.

Before 0.33.2 nine modules carried their own `_conn_from_config`, and accounts.py,
market.py, settings.py and option_stock_link.py called psycopg2.connect inline. The
per-env connects set no connect_timeout, so a host that did not answer hung the
caller. They now all go through `write_support.open_conn` (connect_timeout=10s, the
value the Golden Source connects and `write_connection` already used). Results are
unchanged: not configured -> None / False, connect failure -> None / False (logged).
"""

from __future__ import annotations

import ast
import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

import bifrost_core
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.portfolio.reader import accounts

CFG = {"sink": "postgres", "postgres": {"host": "db.invalid", "database": "bifrost_test"}}

_COPIES = (
    "trade_review",
    "strategy_opportunity_write",
    "strategy_allocation_write",
    "strategy_structure_write",
    "template_config_write",
    "strategy_rules_delete",
    "gate_safety_write",
    "saved_search",
    "strategy_plan",
)


class _Refused(Exception):
    pass


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Every connect attempt (params + golden flag); each one is refused."""
    seen: List[Dict[str, Any]] = []

    def _connect(params: Dict[str, Any], golden: bool = False) -> Any:
        seen.append({**params, "_golden": golden})
        raise _Refused("connection refused")

    monkeypatch.setattr(ws, "connect", _connect)
    return seen


def test_open_conn_sets_the_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    got: List[Any] = []
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: got.append((params, golden)))
    ws.open_conn(CFG)
    ws.open_conn(CFG, golden=True)
    (env, env_golden), (gs, gs_golden) = got
    assert env["connect_timeout"] == 10 and env_golden is False
    assert gs["connect_timeout"] == 10 and gs_golden is True
    assert env["host"] == "db.invalid" and env["dbname"] == "bifrost_test"
    assert ws._CONNECT_TIMEOUT_S == 10


@pytest.mark.parametrize("module", _COPIES)
def test_conn_from_config_not_configured_is_none(module: str, calls: List[Dict[str, Any]]) -> None:
    mod = importlib.import_module(f"bifrost_core.monitor.reader.{module}")
    assert mod._conn_from_config(None) is None
    assert mod._conn_from_config({}) is None
    assert mod._conn_from_config({"sink": "json"}) is None
    assert calls == []


@pytest.mark.parametrize("module", _COPIES)
def test_conn_from_config_failure_is_none_and_logged(
    module: str, calls: List[Dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    mod = importlib.import_module(f"bifrost_core.monitor.reader.{module}")
    with caplog.at_level(logging.WARNING):
        assert mod._conn_from_config(CFG) is None
    assert [c["connect_timeout"] for c in calls] == [10]
    assert calls[0]["_golden"] is False
    assert any(
        r.name == mod.__name__ and f"{module} connect failed" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("module", _COPIES)
def test_conn_from_config_returns_the_connection(
    module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = object()
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: sentinel)
    mod = importlib.import_module(f"bifrost_core.monitor.reader.{module}")
    assert mod._conn_from_config({"postgres": {"host": "db.invalid"}}) is sentinel


# --- accounts.py: same results as before when the connect fails ------------------------

_ACCOUNTS_CASES = [
    # (callable, expected result, expected connects as golden flags in order)
    (lambda: accounts.replace_execution_fill_splits(CFG, 5, []), False, [False]),
    (lambda: accounts.sync_accounts_snapshot_to_db(CFG, [{"account_id": "U0000001"}]), False, [True]),
    (lambda: accounts.write_account_executions_to_db(CFG, [{"exec_id": "x1"}]), False, [True]),
    (lambda: accounts.update_execution_commission(CFG, "x1", 1.0, None, "USD"), False, [True]),
    (
        lambda: accounts.insert_one_execution(
            CFG, {"account_id": "U0000001", "symbol": "ZZZ", "quantity": 1, "price": 1.0}
        ),
        None,
        [False],
    ),
    (lambda: accounts.update_one_execution(CFG, 5, {"price": 2.0}), False, [False]),
    (lambda: accounts.delete_one_execution(CFG, 5), False, [False]),
]


@pytest.mark.parametrize("case", range(len(_ACCOUNTS_CASES)))
def test_accounts_writers_on_connect_failure(case: int, calls: List[Dict[str, Any]]) -> None:
    fn, expected, golden_flags = _ACCOUNTS_CASES[case]
    assert fn() == expected
    assert [c["_golden"] for c in calls] == golden_flags
    assert all(c["connect_timeout"] == 10 for c in calls)


class _Boom(Exception):
    pass


class _RaisingCursor:
    def __enter__(self) -> "_RaisingCursor":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, *args: object, **kwargs: object) -> None:
        raise _Boom("write failed")


class _RaisingConn:
    def __init__(self) -> None:
        self.committed = False
        self.closed = False

    def cursor(self) -> _RaisingCursor:
        return _RaisingCursor()

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def test_upsert_account_transactions_connect_failure_propagates(
    calls: List[Dict[str, Any]],
) -> None:
    """TD-91: a refused connect is not turned into a successful 0-row write."""
    with pytest.raises(_Refused):
        accounts.upsert_account_transactions(CFG, [{"account_id": "U0000001", "report_date": "20260102"}])
    assert [c["_golden"] for c in calls] == [True]
    assert calls[0]["connect_timeout"] == 10


def test_upsert_account_transactions_cursor_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """TD-91 ratchet: a raising cursor propagates and nothing is committed."""
    conn = _RaisingConn()
    monkeypatch.setattr(ws, "open_conn", lambda *args, **kwargs: conn)
    row = {
        "account_id": "U0000001",
        "ts": 1.0,
        "amount": 1.0,
        "type": "other",
        "report_date": "20260102",
    }
    with pytest.raises(_Boom):
        accounts.upsert_account_transactions(CFG, [row])
    assert conn.committed is False
    assert conn.closed is True


def test_upsert_account_transactions_counts_written_and_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    executed: List[Any] = []

    class _Cur:
        def __enter__(self) -> "_Cur":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def execute(self, sql: str, params: Any = None) -> None:
            executed.append(params)

    class _Conn:
        def __init__(self) -> None:
            self.committed = False

        def cursor(self) -> _Cur:
            return _Cur()

        def commit(self) -> None:
            self.committed = True

        def close(self) -> None:
            pass

    conn = _Conn()
    monkeypatch.setattr(ws, "open_conn", lambda *args, **kwargs: conn)
    rows = [
        {"account_id": "U0000001", "ts": 1.0, "amount": 1.0, "type": "other", "report_date": "20260102"},
        {"account_id": "", "ts": 1.0, "amount": 2.0, "type": "other", "report_date": "20260102"},
        {"account_id": "U0000001", "ts": None, "amount": 3.0, "type": "other", "report_date": "20260102"},
        {"account_id": "U0000001", "ts": 2.0, "amount": 4.0, "type": "other", "report_date": ""},
    ]
    assert accounts.upsert_account_transactions(CFG, rows) == (1, 3)
    assert len(executed) == 1
    assert conn.committed is True


def test_flex_transaction_id_picks_the_partial_conflict_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """TD-103: a transaction id and a row without one take different ON CONFLICT targets."""
    executed: List[str] = []

    class _Cur:
        def __enter__(self) -> "_Cur":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def execute(self, sql: str, params: Any = None) -> None:
            executed.append(sql)

    class _Conn:
        def cursor(self) -> _Cur:
            return _Cur()

        def commit(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(ws, "open_conn", lambda *args, **kwargs: _Conn())
    rows = [
        {
            "account_id": "U00011111",
            "ts": 1.0,
            "amount": 1.0,
            "type": "other",
            "report_date": "20260102",
            "flex_transaction_id": "880001",
        },
        {
            "account_id": "U00011111",
            "ts": 2.0,
            "amount": 2.0,
            "type": "other",
            "report_date": "20260102",
        },
    ]
    assert accounts.upsert_account_transactions(CFG, rows) == (2, 0)
    assert len(executed) == 2
    assert (
        "ON CONFLICT (account_id, flex_transaction_id) WHERE flex_transaction_id IS NOT NULL"
        in executed[0]
    )
    assert "amount = EXCLUDED.amount" in executed[0]
    assert "ON CONFLICT (account_id, ts, amount, type, report_date)" in executed[1]
    assert "flex_transaction_id) WHERE" not in executed[1]


# --- ratchet: no new inline connect ----------------------------------------------------

# Modules allowed to call psycopg2.connect directly, and why.
_ALLOWED_INLINE_CONNECT = {
    "monitor/reader/write_support.py",  # the helper itself (`connect` seam)
    "monitor/reader/common.py",  # StatusReader: long-lived, sets its own session timeouts
    "persistence/postgres/postgres_sink.py",  # daemon sink: long-lived, sets its own timeouts (no DDL, TD-45)
}


def test_no_other_module_calls_psycopg2_connect() -> None:
    root = Path(bifrost_core.__file__).resolve().parent
    found = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "psycopg2"
            ):
                found.add(path.relative_to(root).as_posix())
    assert found == _ALLOWED_INLINE_CONNECT


def test_no_module_defines_its_own_conn_from_config_body() -> None:
    """Each `_conn_from_config` (a seam the tests patch) only delegates."""
    for module in _COPIES:
        mod = importlib.import_module(f"bifrost_core.monitor.reader.{module}")
        src = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        (fn,) = [
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_conn_from_config"
        ]
        body = [n for n in fn.body if not isinstance(n, ast.Expr)]  # drop the docstring
        assert len(body) == 1 and isinstance(body[0], ast.Return), module
        assert "ws.conn_from_config" in ast.unparse(body[0]), module
