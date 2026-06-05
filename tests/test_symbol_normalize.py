"""Tests for symbol_normalize.norm_bars_symbol."""

from __future__ import annotations

from bifrost_core.monitor.reader.symbol_normalize import norm_bars_symbol


def test_ascii_caret_vix() -> None:
    assert norm_bars_symbol("^vix") == "^VIX"
    assert norm_bars_symbol("  ^VIX  ") == "^VIX"


def test_fullwidth_caret_maps_to_ascii() -> None:
    # U+FF3E fullwidth circumflex + VIX
    assert norm_bars_symbol("\uff3eVIX") == "^VIX"
