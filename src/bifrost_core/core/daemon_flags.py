"""Environment switches the trading daemon reads."""

from __future__ import annotations

import os

_TRUE = ("1", "true", "yes")


def daemon_broker_writes_off() -> bool:
    """True when this daemon must not write the shared Golden Source raw_broker tables.

    Accounts, positions, open orders and TWS executions live in bifrost_golden_source,
    which every environment shares; the PROD daemon writes them and DEV's must not.
    DEV sets DAEMON_BROKER_WRITES_OFF=1. The old name ACCOUNT_SYNC_DAEMON_ENABLED is
    still honoured: it dates from the retired account-sync daemon (deleted 2026-10-02,
    debt TD-22) and DEV images built before this change only read that name.
    """
    for name in ("DAEMON_BROKER_WRITES_OFF", "ACCOUNT_SYNC_DAEMON_ENABLED"):
        if os.environ.get(name, "").strip().lower() in _TRUE:
            return True
    return False
