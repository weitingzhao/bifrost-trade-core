"""TD-43: one rule for where an instance stands, and the leg / scope rules beside it (core 0.41.0).

Golden cases for ``instance_state.derive_state`` (the Ledger's rule, as Review applies it),
the ``abstract_leg_to_plan_leg`` mapping (TD-44) and the ``scope_type`` vocabulary (TD-71).
Contracts, dates and quantities are made up.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from bifrost_core.monitor.reader import strategy_opportunity_write as opp_write
from bifrost_core.monitor.reader.errors import WriteInvalid
from bifrost_core.monitor.reader.instance_state import (
    CLOSED_STATES,
    INSTANCE_STATES,
    InstanceLeg,
    derive_state,
    instance_states,
    is_closed,
    parse_expiry,
    read_instance_legs,
    today_new_york,
)
from bifrost_core.monitor.schemas.gate_params import (
    AbstractLeg,
    StructureLeg,
    TemplateLeg,
    abstract_leg_to_plan_leg,
)
from bifrost_core.monitor.schemas.strategies import SCOPE_TYPES, OpportunityBody

TODAY = date(2026, 10, 3)


def leg(net: float, expiry: str | None = "20261016", last: date | None = date(2026, 9, 1)) -> InstanceLeg:
    return InstanceLeg(contract_key="ZZZQ|OPT|20261016|80.0|P", expiry=parse_expiry(expiry), net_qty=net, last_fill_on=last)


# --- derive_state --------------------------------------------------------------------------


def test_no_option_fill_is_no_fills_not_closed() -> None:
    assert derive_state([], TODAY) == ("no_fills", None)
    assert not is_closed("no_fills")


def test_every_leg_flat_is_closed_on_the_last_flat_day() -> None:
    legs = [leg(0.0, last=date(2026, 9, 2)), leg(1e-12, last=date(2026, 9, 9))]
    assert derive_state(legs, TODAY) == ("closed", date(2026, 9, 9))


def test_an_open_leg_not_yet_expired_is_open() -> None:
    assert derive_state([leg(-1.0, "20261016")], TODAY) == ("open", None)
    # Expiry day itself: still open (strictly before today is expired).
    assert derive_state([leg(-1.0, "20261003")], TODAY) == ("open", None)


def test_open_legs_all_past_expiry_without_a_closing_fill_read_expired_and_closed() -> None:
    legs = [leg(-1.0, "20260918"), leg(1.0, "2026-09-25"), leg(0.0, last=date(2026, 9, 30))]
    assert derive_state(legs, TODAY) == ("expired", date(2026, 9, 25))
    assert is_closed("expired") and is_closed("closed")


def test_one_open_leg_still_alive_keeps_the_instance_open() -> None:
    assert derive_state([leg(-1.0, "20260918"), leg(-1.0, "20261120")], TODAY) == ("open", None)


def test_an_open_leg_without_a_readable_expiry_never_expires() -> None:
    assert derive_state([leg(2.0, None)], TODAY) == ("open", None)
    assert derive_state([leg(2.0, "next week")], TODAY) == ("open", None)


def test_the_vocabulary() -> None:
    assert INSTANCE_STATES == ("no_fills", "open", "expired", "closed")
    assert CLOSED_STATES == ("expired", "closed")


def test_parse_expiry_and_today() -> None:
    assert parse_expiry("20261016") == date(2026, 10, 16)
    assert parse_expiry("2026-10-16") == date(2026, 10, 16)
    assert parse_expiry(date(2026, 10, 16)) == date(2026, 10, 16)
    assert parse_expiry("202610") is None and parse_expiry(None) is None and parse_expiry("20261340") is None
    # 02:00 UTC on Oct 4 is still Oct 3 in New York.
    assert today_new_york(datetime(2026, 10, 4, 2, 0, tzinfo=timezone.utc)) == date(2026, 10, 3)


class _Cur:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


def test_legs_are_read_per_instance_and_contract() -> None:
    cur = _Cur(
        [
            {"sid": 7, "contract_key": "A", "expiry": "20260918", "net_qty": -1.0, "last_fill_on": date(2026, 9, 1)},
            (7, "B", "20261016", 0.0, date(2026, 9, 2)),
            (8, "C", "20261016", 0.0, date(2026, 9, 3)),
        ]
    )
    legs = read_instance_legs(cur, [7, 8])
    assert sorted(legs) == [7, 8] and [x.contract_key for x in legs[7]] == ["A", "B"]
    sql, params = cur.executed[0]
    assert "strategy_instance_execution" in sql and "brokerage.executions" in sql and "allocated_quantity" in sql
    assert params == {"ids": [7, 8]}
    states = instance_states(_Cur(cur.rows), [7, 8, 9], today=TODAY)
    assert states == {7: ("expired", date(2026, 9, 18)), 8: ("closed", date(2026, 9, 3)), 9: ("no_fills", None)}
    assert instance_states(_Cur([]), []) == {}


# --- TD-44: abstract legs and the one mapping to a plan leg ---------------------------------


def test_template_and_structure_legs_are_one_model() -> None:
    assert TemplateLeg is AbstractLeg and StructureLeg is AbstractLeg
    AbstractLeg.model_validate({"role": "put", "direction": "short", "option_right": "P", "quantity": 1, "sort_order": 0})
    AbstractLeg.model_validate({"role": "underlying", "direction": "long", "option_right": None})
    AbstractLeg.model_validate({"role": "underlying", "direction": "long", "option_right": ""})
    for bad in ({"direction": "sell"}, {"option_right": "X"}, {"role": "stock"}, {"quantity": 0}):
        with pytest.raises(ValueError):
            AbstractLeg.model_validate(bad)


def test_an_option_slot_becomes_a_concrete_plan_leg_in_the_positions_key_format() -> None:
    out = abstract_leg_to_plan_leg(
        {"role": "put", "direction": "short", "option_right": "P", "quantity": 2},
        symbol="zzzq",
        expiry="2026-10-16",
        strike=82.5,
    )
    assert out == {
        "side": "sell",
        "sec_type": "OPT",
        "right": "P",
        "strike": 82.5,
        "expiry": "2026-10-16",
        "ratio": 2,
        "contract_key": "ZZZQ|OPT|20261016|82.5|P",
        "mid_at_plan": None,
        "quote_asof": None,
    }
    whole = abstract_leg_to_plan_leg(AbstractLeg(direction="long", option_right="C"), symbol="ZZZQ", expiry="2026-10-16", strike=80)
    assert whole["side"] == "buy" and whole["contract_key"] == "ZZZQ|OPT|20261016|80.0|C"


def test_a_stock_slot_becomes_a_stock_leg_and_bad_slots_are_refused() -> None:
    out = abstract_leg_to_plan_leg({"role": "underlying", "direction": "long", "option_right": ""}, symbol="ZZZQ")
    assert out["sec_type"] == "STK" and out["side"] == "buy" and out["contract_key"] is None and out["strike"] is None
    with pytest.raises(ValueError, match="direction"):
        abstract_leg_to_plan_leg({"option_right": "C"}, symbol="ZZZQ", expiry="2026-10-16", strike=80)
    with pytest.raises(ValueError, match="expiry and strike"):
        abstract_leg_to_plan_leg({"direction": "short", "option_right": "C"}, symbol="ZZZQ")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        abstract_leg_to_plan_leg({"direction": "short", "option_right": "C"}, symbol="ZZZQ", expiry="20261016", strike=80)
    with pytest.raises(ValueError, match="symbol"):
        abstract_leg_to_plan_leg({"direction": "short", "option_right": ""}, symbol=" ")


# --- TD-71: scope_type --------------------------------------------------------------------


def test_scope_type_vocabulary() -> None:
    assert SCOPE_TYPES == ("watchlist_stk", "explicit_symbols")
    assert opp_write.normalize_scope_type(None) is None
    assert opp_write.normalize_scope_type("  ") is None
    assert opp_write.normalize_scope_type(" explicit_symbols ") == "explicit_symbols"
    for bad in ("symbols", "watchlist", 3):
        with pytest.raises(WriteInvalid):
            opp_write.normalize_scope_type(bad)


def test_watchlist_stk_needs_a_symbol() -> None:
    opp_write.check_scope_symbols("watchlist_stk", ["ZZZQ"])
    opp_write.check_scope_symbols("explicit_symbols", [])
    opp_write.check_scope_symbols(None, [])
    with pytest.raises(WriteInvalid, match="at least one symbol"):
        opp_write.check_scope_symbols("watchlist_stk", [])


def test_the_create_body_takes_the_vocabulary_or_blank() -> None:
    OpportunityBody(name="r", strategy_structure_id=1, scope_type="watchlist_stk", symbols=["ZZZQ"])
    OpportunityBody(name="r", strategy_structure_id=1, scope_type="")
    with pytest.raises(ValueError):
        OpportunityBody(name="r", strategy_structure_id=1, scope_type="symbols")


def test_create_refuses_an_unknown_scope_or_an_empty_watchlist_scope_before_connecting(monkeypatch) -> None:
    monkeypatch.setattr(opp_write, "_conn_from_config", lambda _cfg: pytest.fail("connected"))
    with pytest.raises(WriteInvalid, match="scope_type must be one of"):
        opp_write.create_opportunity({}, {"name": "r", "strategy_structure_id": 1, "scope_type": "symbols"})
    with pytest.raises(WriteInvalid, match="at least one symbol"):
        opp_write.create_opportunity({}, {"name": "r", "strategy_structure_id": 1, "scope_type": "watchlist_stk"})
