"""Qualified table/view names for market FDW schema in per-env databases.

Golden Source ``market.ticker`` is imported via postgres_fdw so readers can
JOIN universe data with public strategy/preference tables on one connection.
"""

from __future__ import annotations

SCHEMA = "market"

MARKET_FOREIGN_TABLES: tuple[str, ...] = ("ticker",)

MARKET_LOCAL_VIEWS: tuple[str, ...] = ("v_us_equity_universe",)
