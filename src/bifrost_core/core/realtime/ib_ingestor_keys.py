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
# Heartbeat HASH field=SYM → unix ts; Gateway/Ingestor prune via list_fresh_on_demand_stk.
IB_INGESTER_ON_DEMAND_STK_TS = "ib:ingester:control:on_demand_stk_ts"
# Default max age for on-demand heartbeats (Market Live polls ~8s; allow brief idle).
ON_DEMAND_STK_DEFAULT_MAX_AGE_SEC = 120

# OPT on-demand cache — must match bifrost-platform-plugin ib_gateway.redis_keys.
IB_OPTION_CACHE_PREFIX = "ib:option:cache:"
IB_OPTION_CACHE_TTL_SEC = 300
IB_OPTION_ON_DEMAND_SET = "ib:option:control:on_demand_opt"
IB_OPTION_ON_DEMAND_TS = "ib:option:control:on_demand_opt_ts"
IB_OPTION_CACHE_META_REFRESH_TS = "ib:option:cache:meta:last_refresh_ts"
# Default max age for OPT on-demand heartbeats (Gateway one-shot loop; FE polls ~8s).
ON_DEMAND_OPT_DEFAULT_MAX_AGE_SEC = 180
