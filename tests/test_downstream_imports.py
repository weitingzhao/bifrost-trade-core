"""Every bifrost_core import path that a downstream repo uses today keeps resolving.

Pinned table, scanned from origin/main on 2026-10-02 (core 0.33.0): bifrost-trade-api a8334ad,
bifrost-trade-worker 7b4c943, bifrost-platform-plugin-flex-query 059d1ea; src/ and scripts/,
including imports inside functions. bifrost-research, bifrost-platform-plugin and
bifrost-platform-plugin-market-data import nothing from bifrost_core.

Core cannot import those repos, so the names are copied here. A failure means a change
in core broke a downstream import: restore the name (or keep an alias) instead of editing
this table -- unless the downstream repo has already stopped importing it.

Each module is imported in a fresh interpreter as well, so an import that only works
because some other module happened to load first (TD-47) shows up here too.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import bifrost_core

_NAMES: dict[str, tuple[str, ...]] = {
    # trade-worker
    "bifrost_core.config.settings": (
        "get_config_for_guards",
        "get_hedge_config",
        "get_risk_config",
        "get_state_space_config",
        "get_structure_config",
    ),
    # trade-api, trade-worker
    "bifrost_core.config.startup": (
        "config_profile_from_resolved_path",
        "get_effective_ib_config",
        "normalize_server_config",
        "read_config",
        "resolve_startup_config_path",
    ),
    # trade-worker
    "bifrost_core.config.yaml_config": (
        "get_effective_ib_config",
    ),
    # plugin-flex-query, trade-api
    "bifrost_core.core.message_center": (
        "MESSAGE_CENTER_MONITOR_CONSUMER",
        "SystemMessageEvent",
        "build_portfolio_tws_executions_fetch_event",
        "consumer_last_id",
        "fetch_materialized_messages",
        "materialize_stream_event",
        "parse_system_message_event",
        "publish_ib_service_stopped_messages",
        "publish_system_message_event",
        "read_stream_events",
        "set_consumer_last_id",
    ),
    # trade-worker
    "bifrost_core.core.ops_lease": (
        "maintain_health_host",
        "ops_profile_from_config",
    ),
    # trade-api, trade-worker
    "bifrost_core.core.realtime": (
        "create_reader_from_config",
        "run_subscribe_loop",
    ),
    # trade-api
    "bifrost_core.core.realtime.ib_ingestor_keys": (
        "IB_INGESTER_ON_DEMAND_STK",
    ),
    # trade-api
    "bifrost_core.core.realtime.on_demand_opt": (
        "ensure_on_demand_opt",
    ),
    # trade-api
    "bifrost_core.core.realtime.on_demand_stk": (
        "ensure_on_demand_stk",
        "normalize_stk_symbols",
        "remove_on_demand_stk",
    ),
    # trade-api
    "bifrost_core.core.realtime.redis_keys": (
        "SUBSCRIBE_CHANNEL_DEFAULT",
    ),
    # trade-api, trade-worker
    "bifrost_core.core.redis_health_keys": (
        "BIFROST_HEALTH_DAEMON_TRADING_ENGINE",
        "BIFROST_HEALTH_IB_ACCOUNT_AGENT",
        "BIFROST_HEALTH_IB_INGESTOR",
        "BIFROST_HEALTH_IB_OPERATOR",
        "ENGINE_OPS_ACTIVE_REDIS_FIELD",
        "LEGACY_BIFROST_HEALTH_DAEMON_TRADING_ENGINE",
        "LEGACY_BIFROST_IB_ACCOUNT_AGENT",
        "LEGACY_BIFROST_IB_INGESTOR",
        "LEGACY_BIFROST_IB_OPERATOR",
        "LEGACY_BIFROST_OPS_TRADING_ENGINE_META",
        "hgetall_ib_account_agent_health",
        "hgetall_ib_ingestor_health",
        "hgetall_ib_operator_health",
        "redis_hash_field_truthy",
    ),
    # plugin-flex-query, trade-api
    "bifrost_core.core.redis_url": (
        "effective_redis_dict",
        "format_redis_url",
        "ib_redis_url_from_config",
        "redis_url_from_config",
    ),
    # trade-api
    "bifrost_core.ib_operator.client": (
        "IbOperatorClient",
        "build_monitor_ib_status",
    ),
    # trade-api
    "bifrost_core.ib_operator.config": (
        "effective_ib_operator_settings",
    ),
    # trade-api
    "bifrost_core.ib_operator.health_redis": (
        "operator_health_dict_to_redis_hash",
        "prune_legacy_operator_health_hash_fields",
    ),
    # trade-api
    "bifrost_core.ib_operator.protocol": (
        "PROTOCOL_VERSION",
        "result_key",
    ),
    # trade-worker
    "bifrost_core.monitor.integrations.daemon_ib_edge": (
        "derive_daemon_ib_heartbeat_from_redis",
    ),
    # trade-api
    "bifrost_core.monitor.integrations.ib_socket_status": (
        "build_ib_socket_status",
    ),
    # trade-api
    "bifrost_core.monitor.integrations.platform_ib_gateway": (
        "annotate_ib_socket_transport",
        "build_platform_ib_gateway_status",
        "derive_daemon_ib_heartbeat_from_redis",
        "detect_ib_transport",
        "is_platform_ib_gateway_health",
    ),
    # plugin-flex-query, trade-api
    "bifrost_core.monitor.reader": (
        "StatusReader",
        "gate_safety_write",
        "insert_one_execution",
        "saved_search",
        "strategy_allocation_write",
        "strategy_instance",
        "strategy_opportunity_write",
        "strategy_plan",
        "strategy_rules_delete",
        "strategy_structure_write",
        "sync_accounts_snapshot_to_db",
        "template_config_write",
        "trade_review",
        "update_one_execution",
        "upsert_account_transactions",
        "watchlist",
        "write_account_executions_to_db",
        "write_control_command",
        "write_heartbeat_interval",
        "write_ib_config",
        "write_run_status",
    ),
    # trade-api
    "bifrost_core.monitor.reader.errors": (
        "ReadFailed",
        "WriteConflict",
        "WriteError",
        "WriteFailed",
        "WriteInvalid",
        "WriteNotFound",
    ),
    # trade-worker
    "bifrost_core.monitor.reader.gate_safety": (
        "get_active_gate_safety_strategy_id",
        "get_active_strategy_structure_id",
        "get_gates_by_id",
    ),
    # trade-api
    "bifrost_core.monitor.reader.ib_config_public": (
        "ib_client_for_api",
        "ib_client_public_defaults",
    ),
    # trade-api
    "bifrost_core.monitor.reader.reference_indices_merge": (
        "augment_reference_indices_with_caret_symbols",
        "merge_reference_indices",
    ),
    # trade-api
    "bifrost_core.monitor.reader.saved_search": (
        "SavedSearchError",
    ),
    # trade-api
    "bifrost_core.monitor.reader.settings": (
        "write_active_strategy_and_gates",
    ),
    # trade-worker
    "bifrost_core.monitor.reader.strategy": (
        "get_structure_by_id",
    ),
    # trade-api
    "bifrost_core.monitor.reader.strategy_plan": (
        "PlanRuleError",
    ),
    # trade-api
    "bifrost_core.monitor.reader.symbol_normalize": (
        "norm_bars_symbol",
    ),
    # trade-api
    "bifrost_core.monitor.redis_url": (
        "ib_redis_url_from_config",
        "redis_url_from_config",
    ),
    # trade-api
    "bifrost_core.monitor.schemas.gate_params": (
        "default_gates",
    ),
    # trade-api
    "bifrost_core.monitor.schemas.strategies": (
        "AllocationBody",
        "AllocationUpdateBody",
        "OpportunityBody",
        "OpportunityUpdateBody",
        "StrategyInstanceCreateBody",
    ),
    # trade-api
    "bifrost_core.monitor.schemas.strategy_plans": (
        "PlanCreateBody",
        "PlanLinkFillBody",
        "PlanUpdateBody",
    ),
    # trade-api
    "bifrost_core.monitor.schemas.trade_reviews": (
        "TradeReviewBody",
    ),
    # trade-api
    "bifrost_core.monitor.self_check": (
        "derive_daemon_self_check",
        "derive_health_roll_up",
        "is_daemon_alive",  # api 0.3.3 (TD-76)
    ),
    # trade-api
    "bifrost_core.monitor.services": (
        "option_strategy_templates",
    ),
    # trade-api
    "bifrost_core.monitor.services.market_jobs": (
        "TOLERANCE_END_SEC_NON_TRADING",
        "TOLERANCE_END_SEC_TRADING_DAY",
        "coverage_status",
        "get_watchlist_stock_symbols",
    ),
    # trade-api
    "bifrost_core.monitor.services.strategy_parsing": (
        "parse_opened_at_to_unix",
        "parse_strategy_instance_ids_csv",
    ),
    # trade-api
    "bifrost_core.observability.prometheus": (
        "instrument_app",
    ),
    # trade-api
    "bifrost_core.persistence.postgres.brokerage_ddl": (
        "ensure_brokerage_schema",
        "setup_fdw_foreign_tables",
        "setup_fdw_market_tables",
    ),
    # plugin-flex-query
    "bifrost_core.persistence.postgres.brokerage_tables": (
        "EXECUTIONS",
        "GOLDEN_SETTINGS_FLEX",
        "SETTINGS_FLEX",
    ),
    # plugin-flex-query, trade-api, trade-worker
    "bifrost_core.persistence.postgres.connection": (
        "_get_conn_params",
        "_get_golden_source_conn_params",
        # public aliases (TD-20, 0.34.0) for downstream to move to
        "get_conn_params",
        "get_golden_source_conn_params",
    ),
    # trade-api
    "bifrost_core.persistence.postgres.ddl": (
        "_ensure_tables",
        "ensure_tables",  # public alias (TD-20, 0.34.0)
    ),
    # trade-api
    "bifrost_core.persistence.postgres.market_tables": (
        "SCHEMA",
    ),
    # trade-worker
    "bifrost_core.persistence.postgres.postgres_sink": (
        "PostgreSQLSink",
    ),
    # trade-worker
    "bifrost_core.persistence.status_sink": (
        "StatusSink",
    ),
    # trade-worker
    "bifrost_core.portfolio": (
        "accounts",
        "symbol_position",
    ),
    # trade-worker
    "bifrost_core.portfolio.ib_edge": (
        "refresh_accounts_from_redis_edge",
    ),
    # trade-worker
    "bifrost_core.portfolio.positions.portfolio": (
        "OptionLeg",
        "get_option_legs",
        "portfolio_delta",
        "portfolio_gamma",
    ),
    # trade-worker
    "bifrost_core.portfolio.positions.position_book": (
        "PositionBook",
    ),
    # trade-api
    "bifrost_core.portfolio.reader": (
        "accounts",
        "instrument_class",
        "position_categories",
    ),
    # trade-api
    "bifrost_core.portfolio.reader.instrument_class": (
        "INSTRUMENT_CLASSES",
        "normalize_instrument_class",
    ),
    # trade-api
    "bifrost_core.portfolio.reader.option_stock_link": (
        "delete_option_stock_link_strict",
        "insert_option_stock_link",
    ),
    # trade-api
    "bifrost_core.portfolio.services.portfolio": (
        "run_model_analysis_for_account",
    ),
    # trade-worker (PY_VOLLIB_AVAILABLE: its tests/test_black_scholes.py)
    "bifrost_core.pricing.black_scholes": (
        "PY_VOLLIB_AVAILABLE",
        "calculate_greeks",
        "delta",
        "gamma",
    ),
    # trade-api
    "bifrost_core.sse.queue_utils": (
        "put_nowait_drop_oldest",
    ),
}



@pytest.mark.parametrize("module", sorted(_NAMES))
def test_downstream_names_resolve(module: str) -> None:
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("bifrost_core"):
            pytest.skip(f"{module} needs {exc.name}, which core does not declare")
        raise
    missing = []
    for name in _NAMES[module]:
        try:
            _from_import(module, name)
        except (ImportError, AttributeError):
            missing.append(name)
    assert not missing, f"{module} no longer provides {missing}"


def _from_import(module: str, name: str) -> object:
    """What `from module import name` does: an attribute, or else a submodule."""
    mod = importlib.import_module(module)
    if hasattr(mod, name):
        return getattr(mod, name)
    return importlib.import_module(f"{module}.{name}")


_FRESH_SCRIPT = """
import importlib, json, sys
failed = []
for module, names in json.loads(sys.argv[1]):
    for key in [k for k in sys.modules if k == "bifrost_core" or k.startswith("bifrost_core.")]:
        del sys.modules[key]
    try:
        exec(f"from {module} import {', '.join(names)}", {})
    except ModuleNotFoundError as exc:
        # Third-party packages core does not declare (fastapi for the api's
        # observability.prometheus) are the downstream's own dependency.
        if not (exc.name or "").startswith("bifrost_core"):
            continue
        failed.append(f"{module}: {exc!r}")
    except Exception as exc:
        failed.append(f"{module}: {exc!r}")
print("\\n".join(failed))
sys.exit(1 if failed else 0)
"""


def test_downstream_imports_from_a_cold_start() -> None:
    """Each module is imported first, with no other bifrost_core module loaded yet."""
    src = str(Path(bifrost_core.__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
    payload = json.dumps(sorted((m, list(n)) for m, n in _NAMES.items()))
    proc = subprocess.run(
        [sys.executable, "-c", _FRESH_SCRIPT, payload], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_public_aliases_are_the_private_objects() -> None:
    """TD-20: the public names are aliases, not copies -- downstream may switch in any order."""
    from bifrost_core.persistence.postgres import connection, ddl

    assert connection.get_conn_params is connection._get_conn_params
    assert connection.get_golden_source_conn_params is connection._get_golden_source_conn_params
    assert ddl.ensure_tables is ddl._ensure_tables


def test_status_reader_config_is_the_private_attribute_read_only() -> None:
    """TD-20: the api reads StatusReader._config in 34 places; .config is the same object."""
    from bifrost_core.monitor.reader.common import StatusReader

    cfg = {"sink": "postgres", "postgres": {"host": "db.invalid"}}
    reader = StatusReader(cfg)
    assert reader.config is reader._config is cfg
    with pytest.raises(AttributeError):
        reader.config = {}  # type: ignore[misc]
