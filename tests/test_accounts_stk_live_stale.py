"""STK position price: prefer stock_day when contract_quote_live is stale."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bifrost_core.portfolio.reader.accounts_helpers import stk_contract_quote_stale_for_positions


def test_stale_when_no_nbbo():
    assert stk_contract_quote_stale_for_positions({"price_bid": None, "price_ask": None, "price_updated_at": datetime.now(timezone.utc)}) is True


def test_stale_when_updated_at_missing():
    assert stk_contract_quote_stale_for_positions({"price_bid": 1.0, "price_ask": 1.1, "price_updated_at": None}) is True


def test_not_stale_fresh_nbbo():
    now = datetime.now(timezone.utc)
    assert stk_contract_quote_stale_for_positions({"price_bid": 1.0, "price_ask": 1.02, "price_updated_at": now}) is False


def test_stale_when_quote_old():
    old = datetime.now(timezone.utc) - timedelta(hours=10)
    assert stk_contract_quote_stale_for_positions({"price_bid": 1.0, "price_ask": 1.02, "price_updated_at": old}) is True
