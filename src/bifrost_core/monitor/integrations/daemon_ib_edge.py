"""Daemon ib_connected re-exports — Platform IB Gateway aware (TIBM2)."""

from bifrost_core.monitor.integrations.platform_ib_gateway import (
    derive_daemon_ib_heartbeat_from_redis,
)

__all__ = ["derive_daemon_ib_heartbeat_from_redis"]
