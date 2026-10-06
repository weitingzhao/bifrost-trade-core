"""Instrument class: the three classes and the refusals, without a database."""

from __future__ import annotations

import pytest

from bifrost_core.monitor.reader.errors import WriteFailed, WriteInvalid
from bifrost_core.portfolio.reader.instrument_class import (
    INSTRUMENT_CLASSES,
    normalize_instrument_class,
    set_instrument_class_strict,
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
    # The input rules come before the connection: None here would otherwise be WriteFailed.
    with pytest.raises(WriteInvalid, match="contract_key is required"):
        set_instrument_class_strict(None, "", "stock")
    with pytest.raises(WriteInvalid, match="must be one of"):
        set_instrument_class_strict(None, "AAA", "bond")
    with pytest.raises(WriteFailed, match="not configured") as down:
        set_instrument_class_strict(None, "AAA", "stock")
    assert down.value.unavailable
