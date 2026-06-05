"""Redis key names for IB ingestor.

Health hash is under ``bifrost:health:ws_ib_ingestor``; subscriptions/channel/ticks stay ``ib:ingester:*``.
"""

from bifrost_core.core.redis_health_keys import BIFROST_HEALTH_IB_INGESTOR

IB_INGESTER_PREFIX = "ib:ingester"
IB_INGESTER_META_HEALTH = BIFROST_HEALTH_IB_INGESTOR
IB_INGESTER_META_SUBSCRIPTIONS = "ib:ingester:meta:subscriptions"
IB_INGESTER_CHANNEL = "ib:ingester:channel"
IB_INGESTER_TICK_PREFIX = "ib:ingester:tick:"
IB_INGESTER_TICK_TTL_SEC = 300
# Redis SET of additional STK symbols (uppercase) for reqMktData beyond watchlist; merged into ingestor subscription budget.
IB_INGESTER_ON_DEMAND_STK = "ib:ingester:control:on_demand_stk"
