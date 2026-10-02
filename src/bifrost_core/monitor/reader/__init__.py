"""Reader package: DB read/write facade. StatusReader and module-level functions re-exported for drop-in use.
Domain split: accounts = snapshot read/write + execution/transaction write; executions = execution/transaction read + performance; position_categories = position category CRUD.

The re-exports are lazy (PEP 562): importing a leaf such as ``monitor.reader.write_support`` or
``monitor.reader.gate_safety`` no longer drags in ``StatusReader`` and, through it, the whole
monitor / portfolio / pricing tree. That fan-out also made a fresh
``import bifrost_core.portfolio.reader.accounts`` fail with a circular ImportError (TD-47).
Every name below still resolves exactly as before -- ``from bifrost_core.monitor.reader import X``,
``reader.X`` and ``reader.<submodule>`` all keep working.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# name -> module that defines it. Kept in step with __all__ by tests/test_import_fanout.py.
_LAZY: dict[str, str] = {
    "StatusReader": "bifrost_core.monitor.reader.common",
    "ReadFailed": "bifrost_core.monitor.reader.errors",
    "WriteConflict": "bifrost_core.monitor.reader.errors",
    "WriteError": "bifrost_core.monitor.reader.errors",
    "WriteFailed": "bifrost_core.monitor.reader.errors",
    "WriteInvalid": "bifrost_core.monitor.reader.errors",
    "WriteNotFound": "bifrost_core.monitor.reader.errors",
    "write_control_command": "bifrost_core.monitor.reader.status",
    "write_heartbeat_interval": "bifrost_core.monitor.reader.status",
    "write_run_status": "bifrost_core.monitor.reader.status",
    "batch_update_execution_strategy": "bifrost_core.portfolio.reader.accounts",
    "delete_one_execution": "bifrost_core.portfolio.reader.accounts",
    "insert_one_execution": "bifrost_core.portfolio.reader.accounts",
    "sync_accounts_snapshot_to_db": "bifrost_core.portfolio.reader.accounts",
    "update_execution_commission": "bifrost_core.portfolio.reader.accounts",
    "update_one_execution": "bifrost_core.portfolio.reader.accounts",
    "upsert_account_transactions": "bifrost_core.portfolio.reader.accounts",
    "write_account_executions_to_db": "bifrost_core.portfolio.reader.accounts",
    "write_ib_config": "bifrost_core.monitor.reader.settings",
}

__all__ = [
    "ReadFailed",
    "StatusReader",
    "WriteConflict",
    "WriteError",
    "WriteFailed",
    "WriteInvalid",
    "WriteNotFound",
    "batch_update_execution_strategy",
    "delete_one_execution",
    "insert_one_execution",
    "sync_accounts_snapshot_to_db",
    "update_execution_commission",
    "update_one_execution",
    "upsert_account_transactions",
    "write_account_executions_to_db",
    "write_control_command",
    "write_heartbeat_interval",
    "write_ib_config",
    "write_run_status",
]


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is not None:
        value = getattr(importlib.import_module(target), name)
        globals()[name] = value  # cache: later lookups skip __getattr__
        return value
    if not name.startswith("_"):
        # The eager package used to import most submodules as a side effect, so
        # `reader.common` / `reader.market` resolved without an explicit import.
        # Keep that working: import the submodule on first attribute access.
        try:
            return importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"{__name__}.{name}":
                raise
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:  # static analysers and IDEs see the eager form
    from bifrost_core.monitor.reader.common import StatusReader
    from bifrost_core.monitor.reader.errors import (
        ReadFailed,
        WriteConflict,
        WriteError,
        WriteFailed,
        WriteInvalid,
        WriteNotFound,
    )
    from bifrost_core.monitor.reader.settings import write_ib_config
    from bifrost_core.monitor.reader.status import (
        write_control_command,
        write_heartbeat_interval,
        write_run_status,
    )
    from bifrost_core.portfolio.reader.accounts import (
        batch_update_execution_strategy,
        delete_one_execution,
        insert_one_execution,
        sync_accounts_snapshot_to_db,
        update_execution_commission,
        update_one_execution,
        upsert_account_transactions,
        write_account_executions_to_db,
    )
