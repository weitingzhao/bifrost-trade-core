"""Reader package: the api's DB/Redis facade ``StatusReader`` and the read/write modules behind it.

Package-level names are only ``StatusReader`` and the read/write outcome classes; every other
function is imported from the module that defines it (``monitor.reader.status``,
``monitor.reader.settings``, ``portfolio.reader.accounts``, ...). core 0.46.0 (TD-80 C1-b)
dropped the package-level re-exports of the accounts / status / settings write functions and the
fallback that resolved ``reader.<submodule>`` without importing it: ``import
bifrost_core.monitor.reader.<submodule>`` (or ``from bifrost_core.monitor.reader import
<submodule>``) is the way to reach a submodule.

The re-exports are lazy (PEP 562): importing a leaf such as ``monitor.reader.write_support`` or
``monitor.reader.gate_safety`` does not drag in ``StatusReader`` and, through it, the whole
monitor / portfolio / pricing tree (TD-47).
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
}

__all__ = [
    "ReadFailed",
    "StatusReader",
    "WriteConflict",
    "WriteError",
    "WriteFailed",
    "WriteInvalid",
    "WriteNotFound",
]


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is not None:
        value = getattr(importlib.import_module(target), name)
        globals()[name] = value  # cache: later lookups skip __getattr__
        return value
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
