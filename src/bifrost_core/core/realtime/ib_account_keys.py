"""Redis keys for IB Account Agent (account-domain events → Redis only; no PostgreSQL).

Snapshot JSON schema (``IB_ACCOUNT_SNAPSHOT_KEY``)::

    {
      "version": int,           # monotonic counter per write
      "updated_at": float,      # epoch seconds
      "host_connected": bool,
      "secondary_connected": bool,
      "open_orders": [ {...}, ... ],   # same shape as daemon open order dicts
      "accounts_snapshot": [           # optional; list of { account_id, summary, positions }
        { "account_id": str, "summary": {}, "positions": [ {...} ] }
      ],
      "last_execution_rows": [ {...} ] # optional recent fills from secondary path
    }

Engine (Daemon) reads this key and persists to PostgreSQL; Agent does not write PG.
"""

from bifrost_core.core.redis_health_keys import BIFROST_HEALTH_IB_ACCOUNT_AGENT

IB_ACCOUNT_AGENT_META_HEALTH = BIFROST_HEALTH_IB_ACCOUNT_AGENT
IB_ACCOUNT_SNAPSHOT_KEY = "ib:account:snapshot:v1"
IB_ACCOUNT_NOTIFY_CHANNEL = "ib:account:notify"

# Redis Stream for incremental consumption by Account Sync Daemon.
# The Agent XADD-s each snapshot alongside the existing SET; consumers use XREADGROUP.
IB_ACCOUNT_STREAM_KEY = "ib:account:stream:v1"
IB_ACCOUNT_STREAM_MAXLEN = 1000
