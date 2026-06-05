"""Accounts/positions/executions from Redis (IB Account Agent snapshot). Used by GsTrading."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def refresh_accounts_data(app: Any) -> None:
    """R-A1: load accounts_snapshot + open_orders + executions from Redis into store and sink."""
    from bifrost_core.portfolio.ib_edge import refresh_accounts_from_redis_edge

    await refresh_accounts_from_redis_edge(app)


async def refresh_secondary_accounts_and_sync(_app: Any) -> None:
    """No-op: secondary accounts are included in IB Account Agent snapshot when configured."""
    return


async def refresh_executions_only(app: Any) -> None:
    """R-A2: refresh snapshot from Redis (includes last_execution_rows)."""
    from bifrost_core.portfolio.ib_edge import refresh_accounts_from_redis_edge

    await refresh_accounts_from_redis_edge(app)


async def refresh_positions(app: Any) -> None:
    """Refresh positions from Redis snapshot (same as full accounts refresh for edge)."""
    from bifrost_core.portfolio.ib_edge import refresh_accounts_from_redis_edge

    await refresh_accounts_from_redis_edge(app)
