"""TD-47: importing a leaf must not drag in the monitor / portfolio / pricing tree.

Before 0.33.1 `monitor/reader/__init__.py` imported StatusReader eagerly, so
`import bifrost_core.persistence.postgres.ddl` loaded 49 core modules (33 of them
monitor/portfolio/pricing, the Black-Scholes model included), and a fresh
`import bifrost_core.portfolio.reader.accounts` failed with a circular ImportError.
These tests run in a fresh interpreter: inside pytest everything is already imported.
"""

from __future__ import annotations

import importlib
import json
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import bifrost_core
import bifrost_core.monitor.reader as reader

_HEAVY = ("bifrost_core.monitor", "bifrost_core.portfolio", "bifrost_core.pricing")


def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    src = str(Path(bifrost_core.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
    return subprocess.run(
        [sys.executable, "-c", script, *args], capture_output=True, text=True, env=env
    )


def _loaded_after(statement: str) -> list[str]:
    proc = _run(
        f"import json, sys\n{statement}\n"
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith('bifrost_core.'))))"
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_ddl_loads_no_monitor_portfolio_or_pricing_module() -> None:
    loaded = _loaded_after("import bifrost_core.persistence.postgres.ddl")
    assert "bifrost_core.persistence.postgres.ddl" in loaded
    heavy = [m for m in loaded if m.startswith(_HEAVY)]
    assert heavy == []
    assert len(loaded) <= 10, loaded  # 6 at 0.33.1; was 49


def test_brokerage_ddl_loads_no_monitor_portfolio_or_pricing_module() -> None:
    loaded = _loaded_after("import bifrost_core.persistence.postgres.brokerage_ddl")
    assert [m for m in loaded if m.startswith(_HEAVY)] == []


def test_reader_package_import_is_lazy() -> None:
    loaded = _loaded_after("import bifrost_core.monitor.reader")
    assert "bifrost_core.monitor.reader.common" not in loaded
    assert not [m for m in loaded if m.startswith(("bifrost_core.portfolio", "bifrost_core.pricing"))]


def test_reader_leaf_import_skips_status_reader() -> None:
    for leaf in ("write_support", "errors", "gate_safety", "strategy_dim_catalog"):
        loaded = _loaded_after(f"import bifrost_core.monitor.reader.{leaf}")
        assert "bifrost_core.monitor.reader.common" not in loaded, leaf
        assert "bifrost_core.portfolio.model.black_scholes" not in loaded, leaf


@pytest.mark.parametrize(
    "first,second",
    [
        ("bifrost_core.portfolio.reader.accounts", "bifrost_core.persistence.postgres.ddl"),
        ("bifrost_core.persistence.postgres.ddl", "bifrost_core.portfolio.reader.accounts"),
        ("bifrost_core.portfolio.reader.accounts", "bifrost_core.monitor.reader"),
        ("bifrost_core.monitor.reader", "bifrost_core.portfolio.reader.accounts"),
        ("bifrost_core.monitor.reader.common", "bifrost_core.portfolio.reader.accounts"),
    ],
)
def test_fresh_import_in_either_order(first: str, second: str) -> None:
    proc = _run(f"import {first}\nimport {second}\n")
    assert proc.returncode == 0, proc.stderr


_EVERY_MODULE_SCRIPT = """
import importlib, sys
failed = []
for module in sys.argv[1:]:
    for key in [k for k in sys.modules if k == "bifrost_core" or k.startswith("bifrost_core.")]:
        del sys.modules[key]
    try:
        importlib.import_module(module)
    except Exception as exc:
        failed.append(f"{module}: {exc!r}")
print("\\\\n".join(failed))
sys.exit(1 if failed else 0)
"""


def test_every_core_module_imports_first() -> None:
    """Each module imported with no other bifrost_core module loaded (catches import cycles)."""
    modules = sorted(
        m.name for m in pkgutil.walk_packages(bifrost_core.__path__, "bifrost_core.")
    )
    assert len(modules) > 100
    proc = _run(_EVERY_MODULE_SCRIPT, *modules)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the lazy facade still exports exactly what the eager one did ---------------------

_EXPORTED_0_33_0 = {
    "ReadFailed",
    "StatusReader",
    "WriteConflict",
    "WriteError",
    "WriteFailed",
    "WriteInvalid",
    "WriteNotFound",
    "batch_update_execution_strategy",
    "delete_one_execution",
    "delete_stock_bars_for_symbol",
    "insert_one_execution",
    "sync_accounts_snapshot_to_db",
    "update_execution_commission",
    "update_one_execution",
    "upsert_account_transactions",
    "write_account_executions_to_db",
    "write_control_command",
    "write_heartbeat_interval",
    "write_ib_config",
    "write_ohlc_bars_to_db",
    "write_run_status",
    "write_stock_bars",
}


def test_reader_exports_unchanged() -> None:
    assert set(reader.__all__) == _EXPORTED_0_33_0
    assert set(reader._LAZY) == _EXPORTED_0_33_0


@pytest.mark.parametrize("name", sorted(_EXPORTED_0_33_0))
def test_reader_export_is_the_defining_object(name: str) -> None:
    defining = importlib.import_module(reader._LAZY[name])
    assert getattr(reader, name) is getattr(defining, name)


def test_reader_star_import_and_dir() -> None:
    ns: dict[str, object] = {}
    exec("from bifrost_core.monitor.reader import *", ns)
    assert _EXPORTED_0_33_0 <= set(ns)
    assert _EXPORTED_0_33_0 <= set(dir(reader))


def test_reader_submodule_attribute_still_resolves_without_explicit_import() -> None:
    proc = _run(
        "import bifrost_core.monitor.reader as r\n"
        "assert r.common.StatusReader is r.StatusReader\n"
        "assert callable(r.market.write_stock_bars)\n"
    )
    assert proc.returncode == 0, proc.stderr


def test_reader_unknown_attribute_raises_attribute_error() -> None:
    with pytest.raises(AttributeError):
        reader.no_such_name  # noqa: B018
    assert not hasattr(reader, "_no_such_private")


def test_wave9_deferred_names_still_resolve() -> None:
    from bifrost_core.monitor.reader import gate_safety, strategy_dim_catalog
    from bifrost_core.monitor.schemas.gate_params import GateParams
    from bifrost_core.persistence.postgres import wave9_migrations as w9

    assert w9._DIM_TYPE_TO_ENUM is strategy_dim_catalog.DIM_TYPE_TO_ENUM
    assert w9.DIM_TYPE_TO_ENUM is strategy_dim_catalog.DIM_TYPE_TO_ENUM
    assert w9.dim_literals_by_type is strategy_dim_catalog.dim_literals_by_type
    assert w9.build_gate_params_from_flat_row is gate_safety.build_gate_params_from_flat_row
    assert w9.GateParams is GateParams
    with pytest.raises(AttributeError):
        w9.no_such_name  # noqa: B018
