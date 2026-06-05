"""Single source of truth for Redis quote key names and pub/sub channel."""

QUOTE_KEY_PREFIX = "quote:"
TICKER_SUBSCRIBED_KEY = "ticker:subscribed"
QUOTE_TTL_SEC = 300
PUB_CHANNEL = "daemon:quotes"
# SSE (Market GET /quotes/stream) subscribes here; IB ingestor PUBLISHes lightweight notices.
SUBSCRIBE_CHANNEL_DEFAULT = "ib:ingester:channel"
