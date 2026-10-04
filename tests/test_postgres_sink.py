"""TradingDaemonSink (the daemon's StatusSink) smoke tests."""

from __future__ import annotations


from bifrost_core.persistence.status_sink import StatusSink
from bifrost_core.persistence.postgres.postgres_sink import TradingDaemonSink


def test_postgres_sink_implements_status_sink():
    assert issubclass(TradingDaemonSink, StatusSink)


def test_postgres_sink_has_write_snapshot():
    assert hasattr(TradingDaemonSink, "write_snapshot")


def test_old_sink_name_is_gone():
    """TD-75: PostgreSQLSink was a one-version alias (0.39.0); 0.46.0 removed it."""
    from bifrost_core.persistence.postgres import postgres_sink

    assert not hasattr(postgres_sink, "PostgreSQLSink")


def test_daemon_health_key_renamed_not_revalued():
    """TD-75 (0.39.0): only the Python name changed; the value is a live Redis key.
    The old name's one-version alias left in 0.46.0; the LEGACY_ key value stays (api normalises it)."""
    from bifrost_core.core import redis_health_keys as k

    assert k.BIFROST_HEALTH_DAEMON_STRATEGY_TRADING == "bifrost:health:daemon_strategy_trading"
    assert not hasattr(k, "BIFROST_HEALTH_DAEMON_TRADING_ENGINE")
    assert k.LEGACY_BIFROST_HEALTH_DAEMON_TRADING_ENGINE == "bifrost:health:daemon_trading_engine"
    assert not hasattr(k, "BIFROST_OPS_TRADING_ENGINE_META")


# --- TD-45 (core 0.35.0): connecting runs no DDL and terminates nothing ---------------

import ast  # noqa: E402
import logging  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, List  # noqa: E402

import pytest  # noqa: E402

from bifrost_core.persistence.postgres import postgres_sink as sink_mod  # noqa: E402

CFG = {
    "postgres": {"host": "db.invalid", "port": 5432, "database": "bifrost_dev", "user": "u"},
    "golden_source": {"host": "db.invalid", "port": 5432, "database": "bifrost_golden_source", "user": "u"},
}


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn

    def execute(self, sql: str, params: Any = None) -> None:
        self.conn.executed.append(" ".join(sql.split()))
        if self.conn.raises is not None and "INSERT" in sql:
            raise self.conn.raises

    def fetchone(self) -> Any:
        return None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self) -> None:
        self.executed: List[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.raises: Any = None

    def cursor(self, **_: Any) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _UndefinedTable(Exception):
    pgcode = "42P01"


@pytest.fixture
def conns(monkeypatch: pytest.MonkeyPatch) -> List[_Conn]:
    made: List[_Conn] = []

    def fake_connect(**_: Any) -> _Conn:
        made.append(_Conn())
        return made[-1]

    monkeypatch.setattr(sink_mod.psycopg2, "connect", fake_connect)
    monkeypatch.setattr(sink_mod.rds, "connect_daemon_state_redis", lambda cfg: None)
    return made


def test_connect_sets_timeouts_and_nothing_else(conns: List[_Conn]) -> None:
    sink = TradingDaemonSink(CFG)
    assert len(conns) == 2 and sink._conn is conns[0] and sink._golden_conn is conns[1]
    for conn in conns:
        assert conn.executed == [
            "SET lock_timeout = '5s'",
            "SET idle_in_transaction_session_timeout = '60s'",
        ]


def test_reconnect_runs_no_ddl_either(conns: List[_Conn]) -> None:
    sink = TradingDaemonSink(CFG)
    sink._conn = None
    sink._golden_conn = None
    assert sink._ensure_conn() and sink._ensure_golden_conn()
    statements = [s for c in conns for s in c.executed]
    assert all(s.startswith("SET ") for s in statements), statements


def test_sink_source_has_no_schema_apply_or_backend_termination() -> None:
    names = set()
    for module in (sink_mod, __import__("bifrost_core.persistence.postgres.connection", fromlist=["x"])):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert "pg_terminate_backend(" not in text
    for gone in ("_ensure_tables", "ensure_tables", "ensure_brokerage_schema", "release_pg_locks_for_tables"):
        assert gone not in names, gone
    # still there for the db-init Job and the scripts
    from bifrost_core.persistence.postgres import brokerage_ddl, ddl

    assert callable(ddl.ensure_tables) and callable(brokerage_ddl.ensure_brokerage_schema)


def test_missing_table_fails_the_write_with_an_error(
    conns: List[_Conn], caplog: pytest.LogCaptureFixture
) -> None:
    sink = TradingDaemonSink(CFG)
    golden = conns[1]
    golden.raises = _UndefinedTable('relation "raw_broker.executions_raw_tws" does not exist')
    with caplog.at_level(logging.WARNING, logger=sink_mod.logger.name):
        sink.write_account_executions([{"exec_id": "x.1", "account_id": "U0000001", "side": "BOT", "quantity": 1}])
    # rollbacks: the liveness check before the write, then the failed write; the only commit
    # is the connect's SETs
    assert golden.rollbacks == 2 and golden.commits == 1
    assert not any("CREATE" in s for s in golden.executed)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "db-init" in errors[0].getMessage()
    assert "raw_broker.executions_raw_tws" in errors[0].getMessage()
