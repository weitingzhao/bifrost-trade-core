"""Tests for reference_indices merge (DB YAML + DB caret symbols)."""

from __future__ import annotations

from bifrost_core.monitor.reader.reference_indices_merge import (
    augment_reference_indices_with_caret_symbols,
    merge_reference_indices,
)


def test_merge_file_overrides_db_same_symbol() -> None:
    db = [{"symbol": "^GSPC", "label": "Old", "polygon_ticker": "X"}]
    file = [{"symbol": "^GSPC", "label": "S&P 500 (SPY)", "polygon_ticker": "SPY"}]
    out = merge_reference_indices(db, file)
    assert len(out) == 1
    assert out[0]["polygon_ticker"] == "SPY"


def test_merge_union_db_only_symbol() -> None:
    db = [{"symbol": "^DJI", "label": "Dow"}]
    file = [{"symbol": "^GSPC", "polygon_ticker": "SPY"}]
    out = merge_reference_indices(db, file)
    syms = [r["symbol"] for r in out]
    assert syms == ["^GSPC", "^DJI"]


def test_augment_adds_caret_from_db_only() -> None:
    refs = [{"symbol": "^GSPC", "label": "x"}]
    out = augment_reference_indices_with_caret_symbols(refs, ["^VIX", "AAPL"])
    syms = [r["symbol"] for r in out]
    assert "^VIX" in syms
    assert not any(r["symbol"] == "AAPL" for r in out)
