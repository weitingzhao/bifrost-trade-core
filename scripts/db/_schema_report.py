"""Schema refresh reporting helpers (table categories for db_refresh_schema)."""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"

EXPECTED_TABLES_BY_CATEGORY: Dict[str, List[str]] = {
    "account": ["account", "account_positions", "account_transactions"],
    "contract": ["contract_quote_live"],
    "daemon": [
        "daemon_auto_operations",
        "daemon_auto_status_current",
        "daemon_auto_status_history",
        "daemon_control",
        "daemon_heartbeat",
        "daemon_open_orders",
        "daemon_run_status",
    ],
    "execution": [
        "account_execution_commissions",
        "account_execution_instance_allocation",
        "account_execution_option_stock_link",
        "account_executions",
        "account_executions_final",
        "account_executions_fly",
        "executions_raw_flex",
        "executions_raw_journal",
        "executions_raw_tws",
    ],
    "gate_safety": [
        "gate_safety_guard",
        "gate_safety_intent",
        "gate_safety_state",
        "gate_safety_strategy",
        "gate_safety_strategy_earnings_dates",
    ],
    "job": ["job_bars_backfill", "job_ticker_reference_state"],
    "option": ["option_contracts", "option_day", "option_min", "option_snapshots"],
    "preference": [
        "preference_market_streams_symbol_order",
        "preference_position_categories",
        "preference_position_category_tags",
        "preference_data_gap_ack",
    ],
    "reference": ["reference_us_holidays"],
    "settings": ["settings", "settings_ib_flex"],
    "stock": [
        "stock_day",
        "stock_min",
        "tickers",
        "ticker_overview",
        "ticker_types",
        "ticker_related_tickers",
    ],
    "strategy": [
        "strategy_allocation",
        "strategy_allocation_opportunity",
        "strategy_dim",
        "strategy_history",
        "strategy_instance",
        "strategy_opportunity",
        "strategy_opportunity_entry_condition",
        "strategy_opportunity_symbol",
        "strategy_structure",
        "strategy_structure_constraint",
        "strategy_structure_leg",
        "strategy_structure_meta",
        "strategy_template",
        "strategy_template_characteristic",
        "strategy_template_leg",
        "strategy_template_param",
    ],
    "watchlist": ["watchlist"],
}

CATEGORY_ORDER = sorted(EXPECTED_TABLES_BY_CATEGORY.keys())
TABLE_TO_CATEGORY: Dict[str, str] = {
    t: cat for cat, tables in EXPECTED_TABLES_BY_CATEGORY.items() for t in tables
}


def _color_enabled(no_color: bool) -> bool:
    if no_color or os.environ.get("NO_COLOR", "").strip():
        return False
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


def _c(no_color: bool, code: str, text: str) -> str:
    return f"\033[{code}m{text}{RESET}" if _color_enabled(no_color) else text


def _progress(msg: str, no_color: bool = False) -> None:
    print(f"{_c(no_color, BOLD + BLUE, '[refresh]')} {msg}", file=sys.stderr, flush=True)


def _step(msg: str, no_color: bool = False) -> None:
    print(f"{_c(no_color, BOLD + MAGENTA, '[step]')} {_c(no_color, CYAN, msg)}", file=sys.stderr, flush=True)


def _log_table(table_name: str, purpose: str, no_color: bool = False) -> None:
    tag = _c(no_color, DIM, "[table]")
    name = _c(no_color, GREEN, table_name)
    print(f"  {tag}   {name}  {_c(no_color, DIM, '--')} {purpose}", file=sys.stderr, flush=True)
