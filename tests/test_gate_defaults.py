"""default_gates() and where a gate's earnings dates travel (TD-72). No live PostgreSQL."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from bifrost_core.monitor.reader import gate_safety
from bifrost_core.monitor.reader.gate_safety_write import _payload_to_metadata_and_params
from bifrost_core.monitor.schemas.gate_params import GateParams, default_gates

# Invented dates, not taken from any environment.
_DATES = ["2030-01-02", "2030-04-03"]


class _RowConn:
    """A connection whose one SELECT returns `row` (the gate reader opens a RealDictCursor)."""

    def __init__(self, row: Optional[Dict[str, Any]]) -> None:
        self.row = row

    def cursor(self, cursor_factory: Any = None) -> Any:
        row = self.row

        class _Cur:
            def __enter__(self) -> "_Cur":
                return self

            def __exit__(self, *exc: Any) -> None:
                return None

            def execute(self, *a: Any, **k: Any) -> None:
                return None

            def fetchone(self) -> Optional[Dict[str, Any]]:
                return row

        return _Cur()


def _stored_row(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The gate_safety_strategy row create_gate_safety would insert for `payload`."""
    metadata, params = _payload_to_metadata_and_params(payload)
    return {"gate_safety_strategy_id": 7, **metadata, "params_json": json.dumps(params)}


def _has_key(obj: Any, key: str) -> bool:
    if isinstance(obj, dict):
        return key in obj or any(_has_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_key(v, key) for v in obj)
    return False


def test_default_gates_is_a_fresh_gates_object():
    # A gate created from the defaults reads back with exactly these gates.
    row = _stored_row({"name": "fresh", "gates": default_gates(), "earnings_dates": _DATES})
    full = gate_safety.get_gate_safety_full_by_id(_RowConn(row), 7)
    assert full is not None
    assert full["gates"] == default_gates()
    assert full["earnings_dates"] == _DATES


def test_default_gates_is_a_gate_created_with_no_gates():
    row = _stored_row({"name": "empty", "gates": {}})
    full = gate_safety.get_gate_safety_full_by_id(_RowConn(row), 7)
    assert full is not None
    assert full["gates"] == default_gates()
    assert full["earnings_dates"] == []


def test_default_gates_carries_no_dates():
    gates = default_gates()
    assert not _has_key(gates, "dates")
    assert set(gates["strategy"]["earnings"]) == {"blackout_days_before", "blackout_days_after"}


def test_default_gates_is_json_and_the_model_defaults():
    gates = default_gates()
    assert json.loads(json.dumps(gates)) == gates
    expected = GateParams().model_dump(mode="json")
    del expected["strategy"]["earnings"]["dates"]
    assert gates == expected


def test_default_gates_returns_a_new_object_each_call():
    a = default_gates()
    a["strategy"]["structure"]["min_dte"] = 1
    assert default_gates()["strategy"]["structure"]["min_dte"] == GateParams().strategy.structure.min_dte


def test_gate_row_sends_dates_once_and_round_trips():
    row = _stored_row({"name": "g", "gates": default_gates(), "earnings_dates": _DATES})
    full = gate_safety.get_gate_safety_full_by_id(_RowConn(row), 7)
    assert full is not None
    assert not _has_key(full["gates"], "dates")
    # A client that sends the row back unchanged is accepted and keeps the dates.
    _, params = _payload_to_metadata_and_params(
        {"name": full["name"], "gates": full["gates"], "earnings_dates": full["earnings_dates"]}
    )
    assert params["strategy"]["earnings"]["dates"] == _DATES


def test_daemon_gates_keep_dates_nested():
    # config['gates'] for get_hedge_config reads strategy.earnings.dates.
    row = _stored_row({"name": "g", "gates": default_gates(), "earnings_dates": _DATES})
    gates = gate_safety.get_gates_by_id(_RowConn(row), 7)
    assert gates is not None
    assert gates["strategy"]["earnings"]["dates"] == _DATES


def test_empty_nested_dates_are_accepted():
    payload = {"name": "g", "gates": {"strategy": {"earnings": {"dates": []}}}, "earnings_dates": _DATES}
    _, params = _payload_to_metadata_and_params(payload)
    assert params["strategy"]["earnings"]["dates"] == _DATES


def test_missing_gate_reads_none():
    assert gate_safety.get_gate_safety_full_by_id(_RowConn(None), 7) is None
    assert gate_safety.get_gates_by_id(_RowConn(None), 7) is None
