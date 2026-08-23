"""Qualified table/view names for market FDW schema in per-env databases.

Golden Source ``raw_market.ticker`` / ``raw_market.us_market_holiday`` /
``raw_market.ticker_related`` are imported into local ``market.*`` via postgres_fdw
so readers can JOIN universe / holiday / related data with public tables on one connection.
"""

from __future__ import annotations

SCHEMA = "market"

MARKET_FOREIGN_TABLES: tuple[str, ...] = ("ticker", "us_market_holiday", "ticker_related")

MARKET_LOCAL_VIEWS: tuple[str, ...] = ("v_us_equity_universe",)
