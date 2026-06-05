"""Unit tests for Massive stock OHLC persistence helpers."""

from __future__ import annotations

from bifrost_core.persistence.postgres.stock_ohlc_massive import timespan_to_stock_period


def test_timespan_to_stock_period_minute() -> None:
    assert timespan_to_stock_period("minute", 1) == "1 min"
    assert timespan_to_stock_period("minute", 5) == "5 mins"


def test_timespan_to_stock_period_hour() -> None:
    assert timespan_to_stock_period("hour", 1) == "1 hour"


def test_timespan_to_stock_period_day() -> None:
    assert timespan_to_stock_period("day", 1) == "1 D"
