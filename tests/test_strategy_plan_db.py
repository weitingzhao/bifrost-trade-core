"""strategy_plan against real Postgres: the DDL, and the transitions end to end.

Marked `db`: `make test` skips it, `make test-all` with PGHOST runs it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from bifrost_core.monitor.reader import strategy_plan
from bifrost_core.monitor.reader.strategy_plan import PlanRuleError

pytestmark = pytest.mark.db


@pytest.fixture
def plans(pg_conn, monkeypatch: pytest.MonkeyPatch):
    """The plan functions, writing through the fixture's own connection.

    `_conn_from_config` is replaced with one that hands back this connection and
    ignores `close()`, so every row this test writes is rolled back at teardown.
    """

    class _Shared:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        def cursor(self, **kw: Any) -> Any:
            return self._conn.cursor(**kw)

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(strategy_plan, "_conn_from_config", lambda _cfg: _Shared(pg_conn))
    return strategy_plan


def _leg() -> Dict[str, Any]:
    return {"side": "sell", "sec_type": "OPT", "right": "P", "strike": 180, "expiry": "2026-11-20"}


def _draft(plans, **over: Any) -> int:
    payload = {
        "account_id": "TEST-PLANS",
        "symbol": "NVDA",
        "structure_label": "Short put",
        "qty": 1,
        "legs": [_leg()],
        "source_kind": "manual",
    }
    payload.update(over)
    plan_id = plans.create_plan({"sink": "postgres"}, payload)
    assert plan_id is not None
    return plan_id


def test_ddl_creates_the_table_and_its_three_indexes(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = 'strategy_plan'
            """
        )
        assert cur.fetchone() is not None
        cur.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = current_schema() AND tablename = 'strategy_plan'
            """
        )
        names = {r[0] for r in cur.fetchall()}
    assert {
        "strategy_plan_status_created",
        "strategy_plan_symbol",
        "strategy_plan_trade",
    } <= names


def test_the_table_refuses_a_status_it_does_not_know(pg_conn) -> None:
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO strategy_plan (account_id, symbol, structure_label, qty, status) "
                "VALUES ('T', 'NVDA', 'Short put', 1, 'submitted')"
            )
    pg_conn.rollback()


def test_a_plan_walks_draft_to_intended_to_filled(plans, pg_conn) -> None:
    plan_id = _draft(plans, target_kind="credit_pct", target_value=50)
    cfg = {"sink": "postgres"}
    assert plans.get_plan(cfg, plan_id)["effective_status"] == "draft"
    assert plans.intend_plan(cfg, plan_id) is True
    row = plans.get_plan(cfg, plan_id)
    assert row["status"] == "intended" and row["intended_at"] is not None

    opened = datetime.now(timezone.utc) - timedelta(hours=2)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO strategy_structure (name) VALUES ('test-plan-structure') "
            "RETURNING strategy_structure_id"
        )
        structure_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO strategy_opportunity (name, strategy_structure_id) "
            "VALUES ('test-plan-opportunity', %s) RETURNING strategy_opportunity_id",
            (structure_id,),
        )
        opportunity_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO trade (strategy_opportunity_id, account_id, opened_at) "
            "VALUES (%s, 'TEST-PLANS', %s) RETURNING trade_id",
            (opportunity_id, opened),
        )
        instance_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO trade (strategy_opportunity_id, account_id, opened_at) "
            "VALUES (%s, 'OTHER-ACCOUNT', %s) RETURNING trade_id",
            (opportunity_id, opened),
        )
        other_instance_id = cur.fetchone()[0]

    with pytest.raises(PlanRuleError, match="belongs to account OTHER-ACCOUNT"):
        plans.link_fill(cfg, plan_id, other_instance_id)
    assert plans.link_fill(cfg, plan_id, instance_id) is True
    row = plans.get_plan(cfg, plan_id)
    assert row["status"] == "filled"
    assert row["trade_id"] == instance_id
    # The fill time is the instance's own open, not the moment someone linked it.
    assert abs((row["filled_at"] - opened).total_seconds()) < 1
    with pytest.raises(PlanRuleError, match="cannot be cancelled"):
        plans.cancel_plan(cfg, plan_id)
    pg_conn.rollback()


def test_an_intent_cannot_be_edited_and_expires_without_changing_status(plans, pg_conn) -> None:
    cfg = {"sink": "postgres"}
    plan_id = _draft(
        plans,
        exit_by="2026-10-16",
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    assert plans.intend_plan(cfg, plan_id) is True
    row = plans.get_plan(cfg, plan_id)
    assert row["status"] == "intended"
    assert row["effective_status"] == "expired"
    with pytest.raises(PlanRuleError, match="only a draft can be edited"):
        plans.update_plan(cfg, plan_id, {"qty": 5})
    # An expired intent is still linkable and still cancellable — it happened.
    assert plans.cancel_plan(cfg, plan_id) is True
    assert plans.get_plan(cfg, plan_id)["status"] == "cancelled"
    pg_conn.rollback()


def test_intend_says_what_is_missing(plans, pg_conn) -> None:
    cfg = {"sink": "postgres"}
    no_exit = _draft(plans)
    with pytest.raises(PlanRuleError, match="nothing to compare the outcome against"):
        plans.intend_plan(cfg, no_exit)
    no_legs = _draft(plans, legs=[], exit_by="2026-10-16")
    with pytest.raises(PlanRuleError, match="at least one leg"):
        plans.intend_plan(cfg, no_legs)
    assert plans.get_plan(cfg, no_exit)["status"] == "draft"
    pg_conn.rollback()


def test_list_filters_by_status_and_symbol(plans, pg_conn) -> None:
    cfg = {"sink": "postgres"}
    _draft(plans, symbol="NVDA", exit_by="2026-10-16")
    mu = _draft(plans, symbol="MU", exit_by="2026-10-16")
    assert plans.intend_plan(cfg, mu) is True
    drafts = plans.list_plans(cfg, status="draft", account_id="TEST-PLANS")
    assert {r["symbol"] for r in drafts} == {"NVDA"}
    intended = plans.list_plans(cfg, status="intended", account_id="TEST-PLANS")
    assert {r["symbol"] for r in intended} == {"MU"}
    assert plans.list_plans(cfg, symbol="mu", account_id="TEST-PLANS")[0]["strategy_plan_id"] == mu
    pg_conn.rollback()


def test_list_filters_by_source_kind_and_ref(plans, pg_conn) -> None:
    """TD-178 (core 0.53.0): exact match on the stored source_kind / source_ref."""
    cfg = {"sink": "postgres"}
    h1 = _draft(plans, source_kind="hypothesis", source_ref="h-1")
    h2 = _draft(plans, source_kind="hypothesis", source_ref="h-2")
    manual = _draft(plans, source_ref="h-1")
    kinds = plans.list_plans(cfg, account_id="TEST-PLANS", source_kind="hypothesis")
    assert {r["strategy_plan_id"] for r in kinds} == {h1, h2}
    one = plans.list_plans(cfg, account_id="TEST-PLANS", source_kind="hypothesis", source_ref="h-1")
    assert [r["strategy_plan_id"] for r in one] == [h1]
    by_ref = plans.list_plans(cfg, account_id="TEST-PLANS", source_ref="h-1")
    assert {r["strategy_plan_id"] for r in by_ref} == {h1, manual}
    # The cap counts filtered rows: the newest manual plan does not take the one slot.
    capped = plans.list_plans(cfg, account_id="TEST-PLANS", source_kind="hypothesis", limit=1)
    assert [r["strategy_plan_id"] for r in capped] == [h2]
    assert plans.list_plans(cfg, account_id="TEST-PLANS", source_kind="roll") == []
    pg_conn.rollback()
