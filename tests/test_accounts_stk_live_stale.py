"""STK position price: the stock_day fallback's previous close for Daily %."""

from __future__ import annotations

from datetime import date, datetime, timezone

from bifrost_core.portfolio.reader.accounts_helpers import resolve_daily_prev_close_from_fallback


def _epoch_utc_midnight(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def test_daily_prev_close_uses_bar_close_when_latest_bar_is_yesterday():
    """HIMS bug: latest bar is Aug 18 while today is Aug 19 → yesterday close is 27.39, not 28.61."""
    today = date(2026, 8, 19)
    bar_epoch = _epoch_utc_midnight(date(2026, 8, 18))
    assert resolve_daily_prev_close_from_fallback(27.39, bar_epoch, 28.61, today=today) == 27.39


def test_daily_prev_close_uses_prev_close_when_latest_bar_is_today():
    today = date(2026, 8, 19)
    bar_epoch = _epoch_utc_midnight(date(2026, 8, 19))
    assert resolve_daily_prev_close_from_fallback(29.07, bar_epoch, 27.39, today=today) == 27.39


def test_daily_prev_close_none_when_today_bar_missing_prev_close():
    today = date(2026, 8, 19)
    bar_epoch = _epoch_utc_midnight(date(2026, 8, 19))
    assert resolve_daily_prev_close_from_fallback(29.07, bar_epoch, None, today=today) is None
