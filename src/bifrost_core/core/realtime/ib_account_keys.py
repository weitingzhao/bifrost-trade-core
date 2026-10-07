"""Redis keys for IB Account Agent (account-domain events → Redis only; no PostgreSQL).

Snapshot JSON schema (``IB_ACCOUNT_SNAPSHOT_KEY``)::

    {
      "version": int,           # monotonic counter per write
      "updated_at": float,      # epoch seconds
      "host_connected": bool,
      "secondary_connected": bool,
      "open_orders": [ {...}, ... ],   # present only after a successful reqOpenOrders;
                                   # absent is not an empty book (TD-211)
      "accounts_snapshot": [           # optional; list of { account_id, summary, positions }
        { "account_id": str, "summary": {}, "positions": [ {...} ] }
      ],
      "last_execution_rows": [ {...} ] # present only after a successful reqExecutions
    }

Engine (Daemon) reads this key and persists to PostgreSQL; the gateway does not write PG.
``open_orders`` is written only when the key is present.
"""

from bifrost_core.core.redis_health_keys import BIFROST_HEALTH_IB_ACCOUNT_AGENT

IB_ACCOUNT_AGENT_META_HEALTH = BIFROST_HEALTH_IB_ACCOUNT_AGENT
IB_ACCOUNT_SNAPSHOT_KEY = "ib:account:snapshot:v1"
IB_ACCOUNT_NOTIFY_CHANNEL = "ib:account:notify"
