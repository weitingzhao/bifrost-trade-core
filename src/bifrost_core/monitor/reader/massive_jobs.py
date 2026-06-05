"""Adapter for Massive-backed option bars in PostgreSQL (thin delegate until vendor code lives under src)."""

from typing import Any, Dict, List


def get_option_bars(
    config: dict,
    symbol: str,
    expiry: str,
    strike: float,
    option_right: str,
    *,
    period: str = "1 min",
    source: str = "massive",
    limit: int = 200,
) -> List[Dict[str, Any]]:
    from src.vendor.massive.reader import get_option_bars as _impl

    return _impl(
        config,
        symbol,
        expiry,
        strike,
        option_right,
        period=period,
        source=source,
        limit=limit,
    )
