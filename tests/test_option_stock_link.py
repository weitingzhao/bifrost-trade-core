"""Unit tests for option ↔ stock link helpers (slippage vs close, underlying symbol)."""

from bifrost_core.portfolio.reader.option_stock_link import (
    slippage_amount_vs_close,
    underlying_symbol_from_row,
)


def test_slippage_amount_vs_close_buy_above_close():
    # +100 sh @ 101 vs close 100 → +100
    assert slippage_amount_vs_close(100.0, 101.0, 100.0) == 100.0


def test_slippage_amount_vs_close_sell_negative_qty():
    # Short 50 @ 99 vs close 100: signed qty -50 → (-50)*(99-100) = +50
    assert slippage_amount_vs_close(-50.0, 99.0, 100.0) == 50.0


def test_slippage_amount_vs_close_missing_close():
    assert slippage_amount_vs_close(10.0, 100.0, None) is None


def test_underlying_symbol_from_row_flex():
    assert underlying_symbol_from_row({"underlying_symbol": "AAPL", "symbol": "AAPL  260320C00150000"}) == "AAPL"


def test_underlying_symbol_from_row_first_token():
    assert underlying_symbol_from_row({"underlying_symbol": "", "symbol": "MSFT  260320P00380000"}) == "MSFT"
