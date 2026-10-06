"""TD-43 / TD-73 (core 0.43.0): three columns core no longer names.

``strategy_plan.filled_at`` (read as the linked instance's ``opened_at``), ``strategy_instance.notes``
and ``trade_review.note`` (a trade's notes live in the Research journal) are dropped by an Owner
db-step after this release (infra ``db-steps.d/2026-10-03-td43-td73-drop-columns``). Until then the
columns exist on DEV / STG / PROD, so core must work with and without them: it never reads or
writes them, and db-init never adds them back. The real-Postgres half is
``test_td43_td73_dropped_columns_db.py``.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from bifrost_core.monitor.reader import strategy_instance, strategy_plan, trade_review
from bifrost_core.monitor.reader.common import StatusReader
from bifrost_core.monitor.reader.errors import WriteInvalid
from bifrost_core.monitor.schemas.strategies import StrategyInstanceCreateBody, StrategyInstanceUpdateBody
from bifrost_core.monitor.schemas.trade_reviews import TradeReviewBody
from write_fakes import FakeConn, Reply

# strategy_instance is ``trade`` since naming R3 (core 0.45.0, persistence/postgres/trade_ddl.py).
DROPPED = {"strategy_plan": "filled_at", "trade": "notes", "trade_review": "note"}
PERSISTENCE = Path(strategy_plan.__file__).resolve().parents[2] / "persistence"


def _create_table_body(text: str, table: str) -> str:
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\s*\)\s*\n\s*\"\"\"", text, re.S)
    assert m, f"no CREATE TABLE for {table}"
    return m.group(1)


def test_fresh_ddl_has_none_of_the_three_columns() -> None:
    ddl = (PERSISTENCE / "postgres" / "trade_ddl.py").read_text(encoding="utf-8")
    for table, column in DROPPED.items():
        body = _create_table_body(ddl, table)
        assert not re.search(rf"^\s*{column}\s", body, re.M), f"{table}.{column} is back in the DDL"


def test_no_migration_adds_them_back() -> None:
    for path in PERSISTENCE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for stmt in re.findall(r"ALTER TABLE\s+(?:IF EXISTS\s+)?(?:public\.)?(\w+)([^;\"]*)", text):
            table, rest = stmt
            column = DROPPED.get(table)
            if column:
                assert not re.search(rf"ADD COLUMN\s+(?:IF NOT EXISTS\s+)?{column}\b", rest), (
                    f"{path.name} adds {table}.{column} back"
                )


def test_plan_reads_filled_at_from_the_instance_only() -> None:
    assert "i.opened_at AS filled_at" in strategy_plan._PLAN_COLUMNS
    assert "p.filled_at" not in strategy_plan._PLAN_COLUMNS
    assert "filled_at" not in strategy_plan._EDITABLE_COLUMNS


def test_review_columns_and_patchables_drop_note() -> None:
    assert not re.search(r"\bnote\b", trade_review._COLUMNS)
    assert "note" not in trade_review.REVIEW_PATCHABLE
    assert "notes" not in strategy_instance.INSTANCE_PATCHABLE


def test_instance_reads_and_create_do_not_name_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_instance, "get_instance_by_id", lambda conn, tid: {"trade_id": tid})
    conn = FakeConn(
        [("SELECT 1 FROM strategy_opportunity", Reply(one=(1,))), ("INSERT INTO trade ", Reply(one=(41,)))]
    )
    row = strategy_instance.create_instance_strict(conn, 7, "U0000001", 1_788_000_000, label="ZZQ put")
    assert row["trade_id"] == 41
    sql, params = conn.statement("INSERT INTO trade ")
    assert "notes" not in sql and len(params) == 4
    monkeypatch.undo()
    conn = FakeConn([("FROM trade si", Reply(one=None))])
    assert strategy_instance.get_instance_by_id(conn, 41) is None
    assert "notes" not in conn.statement("FROM trade si")[0]
    assert "notes" not in inspect.signature(strategy_instance.create_instance_strict).parameters
    # The facade no longer writes (TD-80 C2-b, core 0.48.0): no create method left to carry notes.
    assert not hasattr(StatusReader, "create_strategy_instance")
    assert not hasattr(StatusReader, "create_trade")


@pytest.mark.parametrize("value", ["Rolled early.", None])
def test_a_notes_or_note_key_is_refused_with_where_notes_live(value) -> None:
    conn = FakeConn()
    with pytest.raises(WriteInvalid, match=r"notes was removed in core 0\.43\.0.*Research journal"):
        strategy_instance.patch_instance(conn, 41, {"label": "x", "notes": value})
    with pytest.raises(WriteInvalid, match=r"note was removed in core 0\.43\.0.*Research journal"):
        trade_review.patch_review(conn, 41, {"note": value})
    assert conn.executed == []


def test_save_review_refuses_note_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    monkeypatch.setattr(trade_review, "_conn_from_config", lambda _cfg: conn)
    with pytest.raises(WriteInvalid, match="Research journal"):
        trade_review.save_review({"sink": "postgres"}, 41, {"note": "x"})
    assert conn.executed == []


def test_request_bodies_have_no_note_fields() -> None:
    assert "notes" not in StrategyInstanceCreateBody.model_fields
    assert "notes" not in StrategyInstanceUpdateBody.model_fields
    assert "note" not in TradeReviewBody.model_fields
