"""TD-41 (0.36.0): structures carry no structure_subtype.

The column never existed (the readers selected ``NULL AS structure_subtype``), its label
repeated ``template_display_name``, and the writer's subtype branch picked a covered-call
template that a client now names by ``strategy_template_id``.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict

import pytest

from bifrost_core.monitor.reader import strategy, strategy_structure_write


def test_structure_readers_select_no_subtype() -> None:
    for sql in (strategy._LIST_STRUCTURES_SELECT, inspect.getsource(strategy.get_structure_by_id)):
        assert "structure_subtype" not in sql
    assert "template_display_name" in strategy._LIST_STRUCTURES_SELECT


class _Templates:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def get_template_by_code(self, _conn: Any, code: str) -> Dict[str, Any]:
        self.asked.append(code)
        return {"strategy_template_id": 7, "template_code": code}


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"structure_type": "covered_call"}, "covered_call_otm"),
        # The subtype is no longer read: a bare covered_call is the OTM template.
        ({"structure_type": "covered_call", "structure_subtype": "atm"}, "covered_call_otm"),
        ({"structure_type": "covered_call_itm"}, "covered_call_itm"),
        ({"structure_type": "bull_put_spread"}, "bull_put_spread"),
    ],
)
def test_template_by_name_ignores_a_subtype(
    monkeypatch: pytest.MonkeyPatch, payload: Dict[str, Any], code: str
) -> None:
    fake = _Templates()
    monkeypatch.setattr(strategy_structure_write.template_config, "get_template_by_code", fake.get_template_by_code)
    tid, row = strategy_structure_write._resolve_template_id(object(), payload)
    assert (tid, row["template_code"], fake.asked) == (7, code, [code])
