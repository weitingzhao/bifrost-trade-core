# Daemon IPC (Redis)

Trading Daemon and Account Sync Daemon process IPC no longer uses PostgreSQL.

| Redis key (per-env `redis`) | Type | TTL / maxlen |
|---|---|---|
| `bifrost:daemon:trading:state` | HASH | 180s |
| `bifrost:daemon:trading:control` | STREAM | ~500 |
| `bifrost:daemon:account_sync:state` | HASH | 180s |
| `bifrost:daemon:account_sync:control` | STREAM | ~100 |

Account Sync still **reads** IB account stream from `redis-ib`; it **publishes** heartbeat/control on per-env Redis so Monitor API reads one instance.

Retired `public` tables: `daemon_heartbeat`, `daemon_auto_status_current`, `daemon_auto_status_history`, `daemon_auto_operations`, `daemon_control`, `daemon_run_status`, `account_sync_heartbeat`, `account_sync_control`, `account_sync_run_status`.

Platform data-clone verify and freshness probes use remaining durable `public` tables (`strategy_instance`, `job_bars_backfill`, …), not these IPC keys.

Implementation: `bifrost_core.persistence.redis_daemon_state`.
