"""Qualified table/view names for market FDW schema in per-env databases.

Golden Source ``market.ticker`` / ``market.us_market_holiday`` /
``market.ticker_related`` are imported via postgres_fdw so readers can JOIN
universe / holiday / related data with public strategy/preference tables on
one connection.
"""

from __future__ import annotations

SCHEMA = "market"

MARKET_FOREIGN_TABLES: tuple[str, ...] = ("ticker", "us_market_holiday", "ticker_related")

MARKET_LOCAL_VIEWS: tuple[str, ...] = ("v_us_equity_universe",)
