"""TD-114 / TD-115 ratchets that run in ``make test`` (no database).

* Every commission writer hands the upsert IB's statement sign: Flex as sent, every other
  source (IB API CommissionReport, gateway fills, API/form input) negated.
* The ledger reader's commission does not branch on the execution source any more.
* ``raw_broker.commissions`` is written by one upsert (``persistence.postgres.commissions``);
  before 0.50.0 the INSERT was copied six times.
* Each public writer of ``portfolio.reader.accounts`` has a real-Postgres test (falling allowlist).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.persistence.postgres import commissions as comm
from bifrost_core.persistence.postgres import postgres_sink as sink_mod
from bifrost_core.portfolio.reader import accounts
from bifrost_core.portfolio.reader import executions as executions_reader

SRC = Path(__file__).resolve().parents[1] / "src" / "bifrost_core"
TESTS = Path(__file__).resolve().parent
CFG = {"sink": "postgres"}


@pytest.mark.parametrize(
    "value, source, stored",
    [
        (-0.65, "flex_trades", -0.65),  # Flex charge: as sent
        (0.12, "flex_trades", 0.12),  # Flex rebate: as sent
        (1.05, "tws_client", -1.05),  # IB API cost -> charge
        (1.05, "tws_event", -1.05),
        (1.05, None, -1.05),  # gateway rows stored before 0.35.0 had no source
        (1.5, "journal_closed", -1.5),
        ("0.5", "manual", -0.5),
        (0, "tws_client", 0.0),
        (None, "tws_client", None),
        ("", "flex_trades", None),
    ],
)
def test_stored_commission(value: Any, source: Any, stored: Any) -> None:
    got = comm.stored_commission(value, source)
    assert got == stored
    if got == 0:
        assert str(got) == "0.0"  # never -0.0


def test_reader_commission_is_one_expression() -> None:
    assert executions_reader._COMM_NORM_E == "-c.commission AS commission"
    text = (SRC / "portfolio" / "reader" / "executions.py").read_text(encoding="utf-8")
    assert "THEN c.commission" not in text and "THEN -c.commission" not in text


def test_commissions_insert_lives_in_one_place() -> None:
    pattern = re.compile(r"INSERT INTO \{?(GOLDEN_COMMISSIONS|t)\}? \(exec_id, commission", re.S)
    hits = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"INSERT INTO [^\n]*commissions[^\n]*\(exec_id, commission", text, re.I) or pattern.search(text):
            hits.append(path.relative_to(SRC).as_posix())
    assert hits == ["persistence/postgres/commissions.py"]


# --- every writer hands the upsert the stored sign --------------------------------------------


class _Cur:
    def __init__(self, log: List[Tuple[str, Any]]) -> None:
        self.log = log
        self.description = None

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append((" ".join(sql.split()), params))

    def fetchone(self) -> Any:
        return None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self) -> None:
        self.log: List[Tuple[str, Any]] = []

    def cursor(self, **_: Any) -> _Cur:
        return _Cur(self.log)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _commission_params(conn: _Conn) -> List[Any]:
    return [p[1] for sql, p in conn.log if "raw_broker.commissions" in sql and sql.startswith("INSERT")]


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch) -> _Conn:
    c = _Conn()
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: c)
    return c


def _row(source: str, commission: float) -> dict:
    return {"account_id": "U0000009", "exec_id": f"x.{source}", "source": source, "symbol": "ZQY",
            "sec_type": "STK", "side": "BOT", "quantity": 1.0, "price": 1.0, "commission": commission}


def test_executions_writer_signs_by_source(conn: _Conn) -> None:
    assert accounts.write_account_executions_to_db(
        CFG, [_row("flex_trades", -0.65), _row("tws_client", 1.05), _row("tws_event", 1.0)]
    )
    assert _commission_params(conn) == [-0.65, -1.05, -1.0]


def test_commission_report_is_negated(conn: _Conn) -> None:
    assert accounts.update_execution_commission(CFG, "x.1", 1.05, None, "USD")
    assert _commission_params(conn) == [-1.05]


def test_manual_insert_is_negated(conn: _Conn) -> None:
    accounts.insert_one_execution(
        CFG, {"account_id": "U0000009", "symbol": "ZQY", "quantity": 1, "price": 1.0, "commission": 0.5}
    )
    assert _commission_params(conn) == [-0.5]


def test_daemon_sink_negates(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _Conn()
    monkeypatch.setattr(sink_mod.psycopg2, "connect", lambda **_: c)
    monkeypatch.setattr(sink_mod.rds, "connect_daemon_state_redis", lambda cfg: None)
    sink = sink_mod.TradingDaemonSink(
        {"postgres": {"host": "db.invalid"}, "golden_source": {"host": "db.invalid"}}
    )
    sink.write_account_executions([_row("tws_client", 1.05)])
    sink.update_execution_commission("x.2", 0.7, None, "USD")
    assert _commission_params(c) == [-1.05, -0.7]


# --- TD-115: each public accounts writer has a real-Postgres test ------------------------------

# Writers without one yet. May only shrink: a name that gains a db test must leave this set.
_NO_DB_TEST_YET = {"sync_accounts_snapshot_to_db", "delete_one_execution"}


def test_public_accounts_writers_have_db_tests() -> None:
    db_tests = "\n".join(p.read_text(encoding="utf-8") for p in TESTS.glob("test_*_db.py"))
    missing = {name for name in accounts.__all__ if not re.search(rf"\b{name}\b", db_tests)}
    assert missing == _NO_DB_TEST_YET, (
        "a public writer in portfolio.reader.accounts has no test in tests/*_db.py "
        "(or one in the allowlist gained one: drop it from _NO_DB_TEST_YET)"
    )
