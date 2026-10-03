"""Trade plans: the legs it accepts, the transitions it refuses, and what it never does.

The rules run without a database — they are the reason the module exists — so
they are tested against a fake connection. `test_strategy_plan_db.py` runs the
same transitions against real Postgres.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from bifrost_core.monitor.reader import strategy_plan
from bifrost_core.monitor.reader.strategy_plan import (
    PlanRuleError,
    normalize_plan_legs,
    plan_effective_status,
    plan_exit_is_written,
)

NOW = datetime(2026, 9, 15, 18, 0, tzinfo=timezone.utc)


class _FakeCursor:
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

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, results: Optional[List[Any]] = None, rowcount: int = 1) -> None:
        self.cur = _FakeCursor(results or [], rowcount)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, **_: Any) -> _FakeCursor:
        return self.cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


@pytest.fixture
def conn(monkeypatch: pytest.MonkeyPatch):
    """Hand every plan function the same fake connection."""

    def _make(results: Optional[List[Any]] = None, rowcount: int = 1) -> _FakeConn:
        fake = _FakeConn(results, rowcount)
        monkeypatch.setattr(strategy_plan, "_conn_from_config", lambda _cfg: fake)
        return fake

    return _make


CFG = {"sink": "postgres"}


def _leg(**over: Any) -> Dict[str, Any]:
    base = {"side": "sell", "sec_type": "OPT", "right": "P", "strike": 180, "expiry": "2026-11-20"}
    base.update(over)
    return base


# ── legs ─────────────────────────────────────────────────────────────────


def test_normalises_a_leg_and_defaults_the_ratio() -> None:
    leg = normalize_plan_legs([_leg(side="SELL", right="p", sec_type="opt")])[0]
    assert leg["side"] == "sell"
    assert leg["sec_type"] == "OPT"
    assert leg["right"] == "P"
    assert leg["ratio"] == 1
    # No quote was taken when the plan was written, and that is said, not guessed.
    assert leg["mid_at_plan"] is None
    assert leg["quote_asof"] is None


def test_refuses_a_leg_that_does_not_name_a_contract() -> None:
    with pytest.raises(PlanRuleError, match="right, strike and expiry"):
        normalize_plan_legs([_leg(strike=None)])
    with pytest.raises(PlanRuleError, match="side must be buy or sell"):
        normalize_plan_legs([_leg(side="short")])
    with pytest.raises(PlanRuleError, match="expiry must be YYYY-MM-DD"):
        normalize_plan_legs([_leg(expiry="20261120")])
    with pytest.raises(PlanRuleError, match="ratio must be 1 or more"):
        normalize_plan_legs([_leg(ratio=0)])
    with pytest.raises(PlanRuleError, match="not an object"):
        normalize_plan_legs(["NVDA 2026-11-20 180 P"])


def test_a_stock_leg_needs_no_strike() -> None:
    leg = normalize_plan_legs([{"side": "buy", "sec_type": "STK", "ratio": 100}])[0]
    assert (leg["right"], leg["strike"], leg["expiry"]) == (None, None, None)
    assert leg["ratio"] == 100


# ── effective status ─────────────────────────────────────────────────────


def test_an_intent_past_its_expiry_reads_expired() -> None:
    assert plan_effective_status("intended", NOW - timedelta(hours=1), now=NOW) == "expired"
    assert plan_effective_status("intended", NOW + timedelta(hours=1), now=NOW) == "intended"
    assert plan_effective_status("intended", None, now=NOW) == "intended"


def test_expiry_only_speaks_about_intents() -> None:
    # A draft nobody marked, and a plan that filled, are not "expired": the
    # expiry is about an intent going stale before it was taken.
    assert plan_effective_status("draft", NOW - timedelta(days=5), now=NOW) == "draft"
    assert plan_effective_status("filled", NOW - timedelta(days=5), now=NOW) == "filled"
    assert plan_effective_status("cancelled", NOW - timedelta(days=5), now=NOW) == "cancelled"


def test_an_exit_is_any_one_of_the_three() -> None:
    assert plan_exit_is_written("credit_pct", None, None) is True
    assert plan_exit_is_written(None, "credit_multiple", None) is True
    assert plan_exit_is_written(None, None, "2026-10-16") is True
    assert plan_exit_is_written(None, None, None) is False


# ── transitions ──────────────────────────────────────────────────────────


def test_intend_needs_a_leg_and_an_exit(conn) -> None:
    fake = conn([{"status": "draft", "legs_json": [], "target_kind": None, "stop_kind": None, "exit_by": None}])
    with pytest.raises(PlanRuleError, match="at least one leg"):
        strategy_plan.intend_plan(CFG, 1)
    assert fake.commits == 0

    fake = conn(
        [{"status": "draft", "legs_json": [_leg()], "target_kind": None, "stop_kind": None, "exit_by": None}]
    )
    with pytest.raises(PlanRuleError, match="nothing to compare the outcome against"):
        strategy_plan.intend_plan(CFG, 1)
    assert fake.commits == 0

    fake = conn(
        [
            {
                "status": "draft",
                "legs_json": [_leg()],
                "target_kind": "credit_pct",
                "stop_kind": None,
                "exit_by": None,
            }
        ]
    )
    assert strategy_plan.intend_plan(CFG, 1) is True
    assert fake.commits == 1
    assert "status = 'intended'" in fake.cur.executed[-1][0]


def test_intend_only_moves_a_draft(conn) -> None:
    for status in ("intended", "filled", "cancelled"):
        conn([{"status": status, "legs_json": [_leg()], "target_kind": "credit_pct", "stop_kind": None, "exit_by": None}])
        with pytest.raises(PlanRuleError, match=f"is {status}"):
            strategy_plan.intend_plan(CFG, 1)


def test_a_missing_plan_is_not_a_rule_error(conn) -> None:
    conn([])
    assert strategy_plan.intend_plan(CFG, 404) is False
    conn([])
    assert strategy_plan.cancel_plan(CFG, 404) is False
    conn([])
    assert strategy_plan.update_plan(CFG, 404, {"qty": 2}) is False


def test_an_intent_is_frozen(conn) -> None:
    # The plan is what the outcome gets compared against. Editing it after the
    # fact would remove the comparison, so it is a cancel-and-rewrite or a roll.
    for status in ("intended", "filled", "cancelled"):
        fake = conn([(status,)])
        with pytest.raises(PlanRuleError, match="only a draft can be edited"):
            strategy_plan.update_plan(CFG, 1, {"qty": 3})
        assert fake.commits == 0


def test_a_draft_can_be_edited(conn) -> None:
    fake = conn([("draft",)])
    assert strategy_plan.update_plan(CFG, 1, {"qty": 3, "legs": [_leg()]}) is True
    sql = fake.cur.executed[-1][0]
    assert "qty = %s" in sql and "legs_json = %s::jsonb" in sql and "updated_at = now()" in sql


def test_a_target_needs_both_halves(conn) -> None:
    conn([("draft",)])
    with pytest.raises(PlanRuleError, match="A target needs both a kind and a value"):
        strategy_plan.update_plan(CFG, 1, {"target_kind": "credit_pct"})
    conn([("draft",)])
    with pytest.raises(PlanRuleError, match="A stop needs both a kind and a value"):
        strategy_plan.update_plan(CFG, 1, {"stop_value": 2})


def test_link_fill_refuses_another_account(conn) -> None:
    fake = conn([{"status": "intended", "account_id": "U1"}, {"account_id": "U2", "opened_at": NOW}])
    with pytest.raises(PlanRuleError, match="belongs to account U2"):
        strategy_plan.link_fill(CFG, 1, 7)
    assert fake.commits == 0


def test_link_fill_needs_an_instance_that_exists(conn) -> None:
    conn([{"status": "intended", "account_id": "U1"}])
    with pytest.raises(PlanRuleError, match="No strategy instance 7"):
        strategy_plan.link_fill(CFG, 1, 7)


def test_link_fill_sets_filled_and_the_instance_together(conn) -> None:
    fake = conn([{"status": "intended", "account_id": "U1"}, {"account_id": "U1"}])
    assert strategy_plan.link_fill(CFG, 1, 7) is True
    sql, params = fake.cur.executed[-1]
    assert "status = 'filled'" in sql and "strategy_instance_id = %s" in sql
    # TD-43 (core 0.41.0): filled_at is no longer written; it reads as the instance's opened_at.
    assert "filled_at" not in sql
    assert params == (7, 1)


def test_link_fill_only_follows_an_intent(conn) -> None:
    for status in ("draft", "filled", "cancelled"):
        conn([{"status": status, "account_id": "U1"}])
        with pytest.raises(PlanRuleError, match=f"is {status}"):
            strategy_plan.link_fill(CFG, 1, 7)


def test_cancel_takes_a_draft_or_an_intent_and_nothing_else(conn) -> None:
    for status in ("draft", "intended"):
        fake = conn([(status,)])
        assert strategy_plan.cancel_plan(CFG, 1) is True
        assert "cancelled_at = now()" in fake.cur.executed[-1][0]
    for status in ("filled", "cancelled"):
        fake = conn([(status,)])
        with pytest.raises(PlanRuleError, match=f"is {status}"):
            strategy_plan.cancel_plan(CFG, 1)
        assert fake.commits == 0


def test_create_requires_the_four_things_a_plan_cannot_be_without(conn) -> None:
    conn([(1,)])
    with pytest.raises(PlanRuleError, match="symbol is required"):
        strategy_plan.create_plan(CFG, {"account_id": "U1", "structure_label": "Put", "qty": 1})
    conn([(1,)])
    with pytest.raises(PlanRuleError, match="qty must be 1 or more"):
        strategy_plan.create_plan(
            CFG, {"account_id": "U1", "symbol": "NVDA", "structure_label": "Put", "qty": 0}
        )


def test_create_writes_a_draft_with_the_symbol_upper_cased(conn) -> None:
    fake = conn([(42,)])
    plan_id = strategy_plan.create_plan(
        CFG,
        {
            "account_id": "U1",
            "symbol": "nvda",
            "structure_label": "Short put",
            "qty": 1,
            "legs": [_leg()],
            "source": [{"kind": "symbol", "text": "IV rank 62"}],
        },
    )
    assert plan_id == 42
    sql, params = fake.cur.executed[-1]
    # No status in the insert: the column defaults to draft, and the machine
    # owns every move after that.
    assert "status" not in sql
    assert "NVDA" in params


def test_without_postgres_nothing_is_written(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_plan, "_conn_from_config", lambda _cfg: None)
    assert strategy_plan.create_plan(None, {"account_id": "U1", "symbol": "N", "structure_label": "P", "qty": 1}) is None
    assert strategy_plan.list_plans(None) == []
    assert strategy_plan.get_plan(None, 1) is None
    assert strategy_plan.intend_plan(None, 1) is False


def test_list_filters_and_caps(conn) -> None:
    fake = conn([[]])
    strategy_plan.list_plans(CFG, status="intended", symbol="nvda", account_id="U1", limit=50)
    sql, params = fake.cur.executed[0]
    assert "p.status = %s" in sql and "upper(p.symbol) = upper(%s)" in sql and "p.account_id = %s" in sql
    assert params == ["intended", "nvda", "U1", 50]


def test_rows_carry_the_status_a_reader_should_see(conn) -> None:
    fake = conn(
        [
            [
                {"status": "intended", "expires_at": NOW - timedelta(days=1), "legs_json": None, "source_json": None},
            ]
        ]
    )
    row = strategy_plan.list_plans(CFG)[0]
    assert row["effective_status"] == "expired"
    assert row["status"] == "intended"
    assert row["legs_json"] == [] and row["source_json"] == []
    # One join, for filled_at alone (TD-43): the instance's opened_at. Nothing else about
    # a plan is derived from its instance or its opportunity.
    sql = fake.cur.executed[0][0]
    assert sql.upper().count(" JOIN ") == 1
    assert "i.opened_at AS filled_at" in sql and "LEFT JOIN strategy_instance i" in sql


def test_a_broken_read_is_an_error_not_an_empty_desk(conn, monkeypatch) -> None:
    """A read that fails must not read as "no plans".

    This is not hypothetical: with the table missing in an environment, the
    swallowed error made `{"items": [], "count": 0}` -- a green acceptance check
    for a schema that had never been applied.
    """
    fake = conn([[]])

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError('relation "strategy_plan" does not exist')

    monkeypatch.setattr(fake.cur, "execute", boom)
    with pytest.raises(RuntimeError):
        strategy_plan.list_plans(CFG)
    with pytest.raises(RuntimeError):
        strategy_plan.get_plan(CFG, 1)


def test_no_database_configured_is_still_an_empty_list(monkeypatch) -> None:
    """Not configured is a different thing from broken -- the router maps it to 503."""
    monkeypatch.setattr(strategy_plan, "_conn_from_config", lambda _cfg: None)
    assert strategy_plan.list_plans(None) == []
    assert strategy_plan.get_plan(None, 1) is None


# ── D10 ──────────────────────────────────────────────────────────────────


def test_a_plan_is_never_an_order() -> None:
    """Trading execution is frozen (D10). Nothing here may reach for it."""
    source = inspect.getsource(strategy_plan)
    for forbidden in ("order_intent", "place_order", "ib:operator"):
        assert forbidden not in source, forbidden


# ── delete (Rev .138: the UI holds the call until its Undo toast closes) ──

def test_only_a_draft_can_be_deleted(conn) -> None:
    for status in ("intended", "filled", "cancelled"):
        fake = conn([(status,)])
        with pytest.raises(PlanRuleError, match="only a draft can be deleted"):
            strategy_plan.delete_plan(CFG, 1)
        assert fake.commits == 0
        assert not any(sql.startswith("DELETE") for sql, _ in fake.cur.executed)


def test_a_draft_is_deleted(conn) -> None:
    fake = conn([("draft",)])
    assert strategy_plan.delete_plan(CFG, 7) is True
    assert fake.commits == 1
    assert fake.cur.executed[-1] == ("DELETE FROM strategy_plan WHERE strategy_plan_id = %s", (7,))


def test_deleting_a_missing_plan_is_not_a_rule_error(conn) -> None:
    conn([])
    assert strategy_plan.delete_plan(CFG, 404) is False
