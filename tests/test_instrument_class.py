"""Instrument class: the three classes and the refusals, without a database."""

from __future__ import annotations

from bifrost_core.portfolio.reader.instrument_class import (
    INSTRUMENT_CLASSES,
    delete_instrument_class,
    normalize_instrument_class,
    set_instrument_class,
)


def test_three_classes_in_the_stored_spelling():
    assert INSTRUMENT_CLASSES == ("stock", "fixed_income", "cash_like")
    assert normalize_instrument_class("Fixed income") == "fixed_income"
    assert normalize_instrument_class("cash-like") == "cash_like"
    assert normalize_instrument_class(" STOCK ") == "stock"
    # The Owner's category names are not classes: nothing is inferred from them.
    assert normalize_instrument_class("Fix Income") is None
    assert normalize_instrument_class("") is None


def test_refuses_before_touching_the_database():
    assert set_instrument_class(None, "", "stock") == (False, "contract_key is required.")
    ok, err = set_instrument_class(None, "AAA", "bond")
    assert not ok and "must be one of" in (err or "")
    assert set_instrument_class(None, "AAA", "stock") == (False, "No database connection.")
    assert delete_instrument_class(None, "AAA") is False
