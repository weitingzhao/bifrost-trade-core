"""TD-48: the create / update writers behind POST /strategies/allocations and /opportunities.

These are the older writers (answer an id / a bool) that the TD-15 patch_* writers sit
beside. Owner decision 6 (wave 4, core 0.35.0): a limit or gate id the writer cannot
store is refused with WriteInvalid (the api answers 400); until 0.35.0 it was stored as
NULL without a word. Junk entries in strategy_opportunity_ids are still skipped (not part
of that decision). Ids and names are made up.
"""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_core.monitor.reader import strategy_allocation_write as allocation_write
from bifrost_core.monitor.reader import strategy_opportunity_write as opportunity_write
from bifrost_core.monitor.reader.errors import WriteInvalid
from write_fakes import FakeConn, Reply

CFG = {"sink": "postgres"}


def _use(monkeypatch: pytest.MonkeyPatch, module: Any, conn: Any) -> None:
    monkeypatch.setattr(module, "_conn_from_config", lambda _cfg: conn)


# --- create_allocation ----------------------------------------------------------------


def test_create_allocation_inserts_row_and_links_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn([("INSERT INTO strategy_allocation (", Reply(one=(41,)))])
    _use(monkeypatch, allocation_write, conn)
    aid = allocation_write.create_allocation(
        CFG,
        {
            "name": "  Core sleeve  ",
            "strategy_opportunity_ids": [7, "8", "x", None, 9],
            "gate_safety_strategy_id": "3",
            "allocation_limits": {"max_positions": "4", "max_bp_pct": 12.5},
        },
    )
    assert aid == 41
    _, params = conn.statement("INSERT INTO strategy_allocation (")
    assert params == ("Core sleeve", 3, 4, 12.5, True)
    links = [p for sql, p in conn.executed if "INSERT INTO strategy_allocation_opportunity" in sql]
    assert links == [(41, 7, 0), (41, 8, 1), (41, 9, 2)]  # junk ids skipped, order kept
    assert conn.commits == 1 and conn.closed


def test_create_allocation_clears_limits_sent_as_null(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn([("INSERT INTO strategy_allocation (", Reply(one=(42,)))])
    _use(monkeypatch, allocation_write, conn)
    aid = allocation_write.create_allocation(
        CFG,
        {
            "name": "Sleeve",
            "strategy_opportunity_ids": [],
            "gate_safety_strategy_id": None,
            "allocation_limits": {"max_positions": None, "max_bp_pct": 0},
            "is_active": 0,
        },
    )
    assert aid == 42
    _, params = conn.statement("INSERT INTO strategy_allocation (")
    assert params == ("Sleeve", None, None, 0.0, False)
    assert not conn.ran("INSERT INTO strategy_allocation_opportunity")


BAD_LIMITS = [
    ({"allocation_limits": {"max_positions": "four"}}, "max_positions must be a whole number"),
    ({"allocation_limits": {"max_positions": 2.5}}, "max_positions must be a whole number"),
    ({"allocation_limits": {"max_positions": -1}}, "max_positions must be 0 or more"),
    ({"allocation_limits": {"max_positions": True}}, "max_positions must be a whole number"),
    ({"allocation_limits": {"max_bp_pct": "lots"}}, "max_bp_pct must be a number"),
    ({"allocation_limits": {"max_bp_pct": float("inf")}}, "max_bp_pct must be a finite number"),
    ({"allocation_limits": {"max_bp_pct": -5}}, "max_bp_pct must be 0 or more"),
    ({"allocation_limits": {"max_risk": 3}}, "Unknown allocation_limits field: max_risk"),
    ({"allocation_limits": [3, 20]}, "allocation_limits must be an object"),
    ({"gate_safety_strategy_id": "not-a-number"}, "gate_safety_strategy_id must be a whole number"),
    ({"gate_safety_strategy_id": 0}, "gate_safety_strategy_id must be 1 or more"),
]


@pytest.mark.parametrize("bad,message", BAD_LIMITS)
def test_create_allocation_refuses_what_it_cannot_store(
    bad: dict, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision 6: refused (400 at the api) -- was silently stored as NULL."""
    conn = FakeConn()
    _use(monkeypatch, allocation_write, conn)
    with pytest.raises(WriteInvalid, match=message):
        allocation_write.create_allocation(CFG, {"name": "Sleeve", "strategy_opportunity_ids": [], **bad})
    assert conn.executed == []


@pytest.mark.parametrize("bad,message", BAD_LIMITS)
def test_update_allocation_refuses_what_it_cannot_store(
    bad: dict, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = FakeConn()
    _use(monkeypatch, allocation_write, conn)
    with pytest.raises(WriteInvalid, match=message):
        allocation_write.update_allocation(CFG, 41, bad)
    assert conn.executed == []


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"strategy_opportunity_ids": []}, "name is required"),
        ({"name": " "}, "name is required"),
        ({"name": "A"}, "strategy_opportunity_ids is required"),
        ({"name": "A", "strategy_opportunity_ids": "7"}, "must be a list"),
    ],
)
def test_create_allocation_refuses_bad_payload(
    payload: dict, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = FakeConn()
    _use(monkeypatch, allocation_write, conn)
    with pytest.raises(ValueError, match=message):
        allocation_write.create_allocation(CFG, payload)
    assert conn.executed == []


def test_create_allocation_without_database_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, allocation_write, None)
    assert allocation_write.create_allocation(CFG, {"name": "A", "strategy_opportunity_ids": []}) is None


def test_create_allocation_statement_failure_rolls_back_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn([("INSERT INTO strategy_allocation (", Reply(raises=RuntimeError("boom")))])
    _use(monkeypatch, allocation_write, conn)
    assert allocation_write.create_allocation(CFG, {"name": "A", "strategy_opportunity_ids": [1]}) is None
    assert conn.rollbacks == 1 and conn.commits == 0 and conn.closed


# --- update_allocation ----------------------------------------------------------------


def test_update_allocation_sets_only_what_was_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    _use(monkeypatch, allocation_write, conn)
    assert allocation_write.update_allocation(CFG, 41, {"name": " Renamed ", "is_active": False}) is True
    sql, params = conn.statement("UPDATE strategy_allocation SET")
    assert sql == (
        "UPDATE strategy_allocation SET name = %s, is_active = %s, updated_at = now() "
        "WHERE strategy_allocation_id = %s"
    )
    assert params == ["Renamed", False, 41]
    assert not conn.ran("strategy_allocation_opportunity")
    assert conn.commits == 1


def test_update_allocation_limits_set_both_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PUT's allocation_limits replaces both columns: a limit left out is cleared."""
    conn = FakeConn()
    _use(monkeypatch, allocation_write, conn)
    ok = allocation_write.update_allocation(
        CFG, 41, {"allocation_limits": {"max_positions": "3"}, "gate_safety_strategy_id": None}
    )
    assert ok is True
    sql, params = conn.statement("UPDATE strategy_allocation SET")
    assert "gate_safety_strategy_id = %s, max_positions = %s, max_bp_pct = %s" in sql
    assert params == [None, 3, None, 41]


def test_update_allocation_replaces_links(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    _use(monkeypatch, allocation_write, conn)
    assert allocation_write.update_allocation(CFG, 41, {"strategy_opportunity_ids": [5, 6]}) is True
    assert not conn.ran("UPDATE strategy_allocation SET")  # nothing else sent
    assert conn.statement("DELETE FROM strategy_allocation_opportunity")[1] == (41,)
    links = [p for sql, p in conn.executed if "INSERT INTO strategy_allocation_opportunity" in sql]
    assert links == [(41, 5, 0), (41, 6, 1)]


def test_update_allocation_missing_row_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn([("UPDATE strategy_allocation SET", Reply(rowcount=0))])
    _use(monkeypatch, allocation_write, conn)
    assert allocation_write.update_allocation(CFG, 404, {"name": "A", "strategy_opportunity_ids": [1]}) is False
    assert conn.rollbacks == 1 and conn.commits == 0
    assert not conn.ran("strategy_allocation_opportunity")


def test_update_allocation_empty_payload_and_blank_name(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    _use(monkeypatch, allocation_write, conn)
    assert allocation_write.update_allocation(CFG, 41, {}) is False
    with pytest.raises(ValueError, match="name cannot be empty"):
        allocation_write.update_allocation(CFG, 41, {"name": "  "})
    assert conn.executed == []


# --- create_opportunity / update_opportunity -------------------------------------------


def test_create_opportunity_writes_normalised_json(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn([("INSERT INTO strategy_opportunity (", Reply(one=(17,)))])
    _use(monkeypatch, opportunity_write, conn)
    oid = opportunity_write.create_opportunity(
        CFG,
        {
            "name": " Earnings fade ",
            "strategy_structure_id": "5",
            "default_gate_safety_strategy_id": "4",
            "scope_type": " ",
            "symbols": [" ZZZA ", "", None, "ZZZB"],
            "entry_conditions": [
                {"condition_type": "iv_rank_min", "value_numeric": "40"},
                {"value_text": "no type"},
                {"condition_type": "note", "value_text": "after print"},
            ],
        },
    )
    assert oid == 17
    _, params = conn.statement("INSERT INTO strategy_opportunity (")
    name, structure_id, gate_id, scope, active, symbols_json, conditions_json = params
    assert (name, structure_id, gate_id, scope, active) == ("Earnings fade", 5, 4, None, True)
    assert symbols_json == '["ZZZA", "ZZZB"]'
    assert conditions_json == (
        '[{"condition_type": "iv_rank_min", "value_text": null, "value_numeric": 40.0, "sort_order": 0}, '
        '{"condition_type": "note", "value_text": "after print", "value_numeric": null, "sort_order": 2}]'
    )
    assert conn.commits == 1 and conn.closed


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"strategy_structure_id": 1}, "name is required"),
        ({"name": "A"}, "strategy_structure_id is required"),
        ({"name": "A", "strategy_structure_id": "x"}, "must be an integer"),
    ],
)
def test_create_and_update_opportunity_refuse_bad_payload(
    payload: dict, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = FakeConn()
    _use(monkeypatch, opportunity_write, conn)
    with pytest.raises(ValueError, match=message):
        opportunity_write.create_opportunity(CFG, payload)
    with pytest.raises(ValueError, match=message):
        opportunity_write.update_opportunity(CFG, 17, payload)
    assert conn.executed == []


@pytest.mark.parametrize("gate", ["bad", 0, -2, 1.5, True])
def test_opportunity_writers_refuse_a_bad_gate_id(gate: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner decision 6: refused (400 at the api) -- was silently stored as NULL."""
    conn = FakeConn()
    _use(monkeypatch, opportunity_write, conn)
    payload = {"name": "A", "strategy_structure_id": 5, "default_gate_safety_strategy_id": gate}
    with pytest.raises(WriteInvalid, match="default_gate_safety_strategy_id"):
        opportunity_write.create_opportunity(CFG, payload)
    with pytest.raises(WriteInvalid, match="default_gate_safety_strategy_id"):
        opportunity_write.update_opportunity(CFG, 17, payload)
    assert conn.executed == []


def test_update_opportunity_keeps_stored_json_when_not_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn(
        [("SELECT symbols_json, entry_conditions_json", Reply(one=('["ZZZA"]', [{"condition_type": "x"}])))]
    )
    _use(monkeypatch, opportunity_write, conn)
    ok = opportunity_write.update_opportunity(CFG, 17, {"name": "A", "strategy_structure_id": 5})
    assert ok is True
    _, params = conn.statement("UPDATE strategy_opportunity SET name = %s")
    assert params == ("A", 5, None, None, True, 17)  # is_active not sent -> True
    _, json_params = conn.statement("SET symbols_json = %s::jsonb")
    assert json_params == ('["ZZZA"]', '[{"condition_type": "x"}]', 17)


def test_update_opportunity_replaces_sent_json_without_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    _use(monkeypatch, opportunity_write, conn)
    ok = opportunity_write.update_opportunity(
        CFG,
        17,
        {"name": "A", "strategy_structure_id": 5, "symbols": ["ZZZC"], "entry_conditions": [], "is_active": False},
    )
    assert ok is True
    assert not conn.ran("SELECT symbols_json")
    assert conn.statement("SET symbols_json = %s::jsonb")[1] == ('["ZZZC"]', "[]", 17)
    assert conn.statement("UPDATE strategy_opportunity SET name = %s")[1][4] is False


def test_update_opportunity_missing_row_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn([("UPDATE strategy_opportunity SET name", Reply(rowcount=0))])
    _use(monkeypatch, opportunity_write, conn)
    assert opportunity_write.update_opportunity(CFG, 404, {"name": "A", "strategy_structure_id": 5}) is False
    assert conn.rollbacks == 1 and not conn.ran("SET symbols_json")


def test_opportunity_writers_without_database(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, opportunity_write, None)
    payload = {"name": "A", "strategy_structure_id": 5}
    assert opportunity_write.create_opportunity(CFG, payload) is None
    assert opportunity_write.update_opportunity(CFG, 17, payload) is False
