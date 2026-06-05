"""Connection and StatusReader facade. Delegates to domain modules (status, watchlist, market, settings, accounts, executions, position_categories)."""

import logging
import threading
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import psycopg2

from bifrost_core.persistence.postgres.connection import _get_conn_params

from bifrost_core.config.startup import get_effective_ib_config

from bifrost_core.portfolio.reader import accounts as accounts_module
from bifrost_core.portfolio.reader import executions as executions_module
from bifrost_core.portfolio.reader import option_stock_link as option_stock_link_module
from bifrost_core.monitor.reader import gate_safety as gate_safety_module
from bifrost_core.monitor.reader import market as market_module
from bifrost_core.monitor.reader import strategy as strategy_module
from bifrost_core.monitor.reader import strategy_instance as strategy_instance_module
from bifrost_core.monitor.reader import strategy_win_rate as strategy_win_rate_module
from bifrost_core.monitor.reader import template_config as template_config_module
from bifrost_core.portfolio.reader import position_categories as position_categories_module
from bifrost_core.monitor.reader import settings as settings_module
from bifrost_core.monitor.reader import status as status_module
from bifrost_core.monitor.reader import watchlist as watchlist_module

logger = logging.getLogger(__name__)


class StatusReader:
    """Read daemon_auto_status_current and daemon_auto_operations from PostgreSQL. Uses the same root postgres config as daemon.
    Holds connection and delegates to domain modules (status, watchlist, market, settings, accounts)."""

    def __init__(self, status_config: dict) -> None:
        self._config = status_config
        # Each uvicorn worker thread gets its own DB connection via thread-local.
        self._local = threading.local()

    @property
    def _conn(self) -> Any:
        return getattr(self._local, "conn", None)

    @_conn.setter
    def _conn(self, value: Any) -> None:
        self._local.conn = value

    def _ensure_conn(self) -> bool:
        """Compat helper mirroring PostgreSQLSink._ensure_conn()."""
        return self._connect()

    def _connect(self) -> bool:
        if self._conn is not None:
            try:
                self._conn.rollback()
                return True
            except Exception:
                self._conn = None
        try:
            params = _get_conn_params(self._config)
            self._conn = psycopg2.connect(**params)
            with self._conn.cursor() as cur:
                cur.execute("SET lock_timeout = '5s'")
                cur.execute("SET idle_in_transaction_session_timeout = '30s'")
            self._conn.commit()
            return True
        except Exception as e:
            logger.warning("StatusReader connect failed: %s", e)
            return False

    def _end_read_txn(self) -> None:
        """End any implicit read transaction to avoid idle-in-transaction between requests."""
        if self._conn is not None:
            try:
                self._conn.rollback()
            except Exception:
                pass

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # --- Status domain (delegate to status module) ---
    def get_status_current(self) -> Optional[Dict[str, Any]]:
        if not self._connect():
            return None
        result = status_module.get_status_current(self._conn)
        self._end_read_txn()
        return result

    def get_run_status(self) -> Optional[bool]:
        if not self._connect():
            return None
        result = status_module.get_run_status(self._conn)
        self._end_read_txn()
        return result

    def get_daemon_heartbeat(self) -> Optional[Dict[str, Any]]:
        if not self._connect():
            return None
        result = status_module.get_daemon_heartbeat(self._conn)
        self._end_read_txn()
        return result

    def get_account_sync_heartbeat(self) -> Optional[Dict[str, Any]]:
        if not self._connect():
            return None
        result = status_module.get_account_sync_heartbeat(self._conn)
        self._end_read_txn()
        return result

    def get_operations(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        type_filter: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = status_module.get_operations(
            self._conn, since_ts=since_ts, until_ts=until_ts, type_filter=type_filter, limit=limit
        )
        self._end_read_txn()
        return result

    def get_open_orders(self) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = status_module.get_open_orders(self._conn)
        self._end_read_txn()
        return result

    # --- Watchlist domain (delegate to watchlist module) ---
    def get_watchlist(self) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = watchlist_module.get_watchlist(self._conn)
        self._end_read_txn()
        return result

    def add_watchlist(
        self,
        contract_key: str,
        symbol: Optional[str] = None,
        sec_type: Optional[str] = None,
        expiry: Optional[str] = None,
        strike: Optional[float] = None,
        option_right: Optional[str] = None,
        display_label: Optional[str] = None,
        source: str = "manual",
        category_id: Optional[int] = None,
        optionable: Optional[bool] = None,
    ) -> bool:
        if not self._connect():
            return False
        return watchlist_module.add_watchlist(
            self._conn, contract_key, symbol, sec_type, expiry, strike, option_right, display_label, source, category_id, optionable
        )

    def delete_watchlist(self, contract_key: Optional[str] = None) -> bool:
        if not self._connect():
            return False
        return watchlist_module.delete_watchlist(self._conn, contract_key=contract_key)

    # --- Market domain (delegate to market module) ---
    def get_is_us_trading_day(self, date_str: str) -> bool:
        if not self._connect():
            return True
        result = market_module.get_is_us_trading_day_conn(self._conn, date_str)
        self._end_read_txn()
        return result

    def get_market_holidays(self, exchange: Optional[str] = None, year: Optional[int] = None) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = market_module.get_market_holidays_conn(self._conn, exchange=exchange, year=year)
        self._end_read_txn()
        return result

    def get_contract_quotes(self, contract_keys: List[str]) -> List[Dict[str, Any]]:
        """Return bid/ask/last/mid from contract_quote_live for given contract_keys. Used by GET /quotes for OPT rows."""
        if not self._connect():
            return []
        result = market_module.get_contract_quotes_conn(self._conn, contract_keys)
        self._end_read_txn()
        return result

    def add_market_holiday(
        self, date_str: str, label: Optional[str] = None, exchange: str = "NYSE"
    ) -> bool:
        if not self._connect():
            return False
        return market_module.add_market_holiday_conn(self._conn, date_str, label=label, exchange=exchange)

    def delete_market_holiday(self, date_str: str, exchange: str = "NYSE") -> bool:
        if not self._connect():
            return False
        return market_module.delete_market_holiday_conn(self._conn, date_str, exchange=exchange)

    def get_bars(
        self,
        symbol: Optional[str] = None,
        period: str = "1 D",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = market_module.get_bars(self._conn, symbol=symbol, period=period, limit=limit)
        self._end_read_txn()
        return result

    def get_bars_latest(self, symbol: Optional[str] = None, period: str = "1 D") -> Optional[float]:
        if not self._connect():
            return None
        result = market_module.get_bars_latest(self._conn, symbol=symbol, period=period)
        self._end_read_txn()
        return result

    def get_bar_times_in_range(
        self,
        symbol: Optional[str] = None,
        period: str = "1 D",
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
    ) -> List[float]:
        if not self._connect():
            return []
        result = market_module.get_bar_times_in_range(
            self._conn, symbol=symbol, period=period, start_ts=start_ts, end_ts=end_ts
        )
        self._end_read_txn()
        return result

    def get_bars_benchmark(
        self,
        symbols: Optional[List[str]] = None,
        on_or_before: Optional[date] = None,
    ) -> Dict[str, Dict[str, Any]]:
        if not self._connect():
            return {}
        result = market_module.get_bars_benchmark(self._conn, symbols=symbols, on_or_before=on_or_before)
        self._end_read_txn()
        return result

    def get_stock_day_fallback_price(self, symbol: str) -> Optional[Tuple[float, float, Optional[float]]]:
        if not self._connect():
            return None
        result = market_module.get_stock_day_fallback_price(self._conn, symbol)
        self._end_read_txn()
        return result

    def get_bars_stats(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        if not self._connect():
            return {"stock_day": 0, "stock_min": {}}
        result = market_module.get_bars_stats(self._conn, symbol=symbol)
        self._end_read_txn()
        return result

    def get_option_bars(
        self,
        symbol: str,
        expiry: str,
        strike: float,
        option_right: str,
        period: str = "1 min",
        source: str = "massive",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        from bifrost_core.monitor.reader import massive_jobs as massive_jobs_module

        return massive_jobs_module.get_option_bars(
            self._config,
            symbol,
            expiry,
            strike,
            option_right,
            period=period,
            source=source,
            limit=limit,
        )

    def get_bars_coverage(self, symbols: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = market_module.get_bars_coverage(self._conn, symbols=symbols)
        self._end_read_txn()
        return result

    def get_distinct_caret_bar_symbols(self) -> List[str]:
        """Symbols starting with ``^`` present in ``stock_day`` / ``stock_min`` (for coverage when config lags DB)."""
        if not self._connect():
            return []
        result = market_module.distinct_caret_symbols_in_stock_bars_tables(self._conn)
        self._end_read_txn()
        return result

    # --- Settings domain (delegate to settings module) ---
    def get_ib_config(self) -> Optional[Dict[str, Any]]:
        """Return merged IB config: host/port/client IDs from config.yaml; DB supplies ib_host_account_id, flex_*, stream_*."""
        ib_eff = get_effective_ib_config(self._config)
        db_cfg: Optional[Dict[str, Any]] = None
        if self._connect():
            db_cfg = settings_module.get_ib_config(self._conn)
            self._end_read_txn()
        merged = dict(ib_eff)
        if db_cfg:
            for key in (
                "ib_host_account_id",
                "flex_default_range_days",
                "flex_init_range_days",
                "stream_host_account_id",
                "stream_secondary_account_id",
            ):
                if key in db_cfg:
                    merged[key] = db_cfg[key]
        else:
            merged.setdefault("ib_host_account_id", None)
            merged.setdefault("flex_default_range_days", 30)
            merged.setdefault("flex_init_range_days", 360)
            merged.setdefault("stream_host_account_id", None)
            merged.setdefault("stream_secondary_account_id", None)
        return merged

    def get_flex_config(self, purpose: Optional[str] = None) -> Any:
        if not self._connect():
            return [] if purpose is not None else {"host_token": None, "secondary_token": None, "rows": []}
        result = settings_module.get_flex_config(self._conn, purpose=purpose)
        self._end_read_txn()
        return result

    def get_flex_default_range_dates(self) -> Tuple[str, str]:
        if not self._connect():
            return ("", "")
        result = settings_module.get_flex_default_range_dates(self._conn)
        self._end_read_txn()
        return result

    def get_flex_executions_stats(self) -> Dict[str, Any]:
        if not self._connect():
            return {"count": 0, "accounts": 0, "min_date": None, "max_date": None}
        result = settings_module.get_flex_executions_stats(self._conn)
        self._end_read_txn()
        return result

    def get_flex_init_range_dates(self) -> Tuple[str, str]:
        if not self._connect():
            return ("", "")
        result = settings_module.get_flex_init_range_dates(self._conn)
        self._end_read_txn()
        return result

    # --- Gate safety (strategy & safety boundary from DB) ---
    def get_gates_by_id(self, gate_safety_strategy_id: int) -> Optional[Dict[str, Any]]:
        """Return gates dict (shape of config['gates']) for the given boundary set id. None if missing."""
        if not self._connect():
            return None
        result = gate_safety_module.get_gates_by_id(self._conn, gate_safety_strategy_id)
        self._end_read_txn()
        return result

    def get_active_gate_safety_strategy_id(self) -> Optional[int]:
        """Return settings.active_gate_safety_strategy_id for id=1, or None."""
        if not self._connect():
            return None
        result = gate_safety_module.get_active_gate_safety_strategy_id(self._conn)
        self._end_read_txn()
        return result

    def get_active_strategy_structure_id(self) -> Optional[int]:
        """Return settings.active_strategy_structure_id for id=1, or None."""
        if not self._connect():
            return None
        result = gate_safety_module.get_active_strategy_structure_id(self._conn)
        self._end_read_txn()
        return result

    def get_active_strategy_allocation_id(self) -> Optional[int]:
        """Return settings.active_strategy_allocation_id for id=1, or None."""
        if not self._connect():
            return None
        result = gate_safety_module.get_active_strategy_allocation_id(self._conn)
        self._end_read_txn()
        return result

    def get_gate_safety_name(self, gate_safety_strategy_id: int) -> Optional[str]:
        """Return name of gate_safety_strategy row, or None."""
        if not self._connect():
            return None
        result = gate_safety_module.get_gate_safety_name(self._conn, gate_safety_strategy_id)
        self._end_read_txn()
        return result

    def list_gate_safety_sets(self) -> List[Dict[str, Any]]:
        """Return list of gate_safety_strategy rows for management dropdown."""
        if not self._connect():
            return []
        result = gate_safety_module.list_gate_safety_sets(self._conn)
        self._end_read_txn()
        return result

    def get_gate_safety_full_by_id(self, gate_safety_strategy_id: int) -> Optional[Dict[str, Any]]:
        """Return full gate set for UI edit: metadata + gates + earnings_dates. None if not found."""
        if not self._connect():
            return None
        result = gate_safety_module.get_gate_safety_full_by_id(self._conn, gate_safety_strategy_id)
        self._end_read_txn()
        return result

    def get_structure_by_id(self, strategy_structure_id: int) -> Optional[Dict[str, Any]]:
        """Return one strategy_structure row as dict, or None."""
        if not self._connect():
            return None
        result = strategy_module.get_structure_by_id(self._conn, strategy_structure_id)
        self._end_read_txn()
        return result

    def list_structures(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """Return list of strategy_structure rows."""
        if not self._connect():
            return []
        result = strategy_module.list_structures(self._conn, active_only=active_only)
        self._end_read_txn()
        return result

    def list_dims_grouped(self) -> Dict[str, List[Dict[str, Any]]]:
        if not self._connect():
            return {}
        result = template_config_module.list_dims_grouped(self._conn)
        self._end_read_txn()
        return result

    def list_dims_for_type(self, dim_type: str) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = template_config_module.list_dims_by_type(self._conn, dim_type)
        self._end_read_txn()
        return result

    def list_templates(self, active_only: bool = True) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = template_config_module.list_templates(self._conn, active_only=active_only)
        self._end_read_txn()
        return result

    def get_template_detail(self, strategy_template_id: int) -> Optional[Dict[str, Any]]:
        if not self._connect():
            return None
        result = template_config_module.get_template_detail(self._conn, strategy_template_id)
        self._end_read_txn()
        return result

    def list_opportunities(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """Return list of strategy_opportunity rows (with structure_name from JOIN)."""
        if not self._connect():
            return []
        result = strategy_module.list_opportunities(self._conn, active_only=active_only)
        self._end_read_txn()
        return result

    def get_opportunity_by_id(self, strategy_opportunity_id: int) -> Optional[Dict[str, Any]]:
        """Return one strategy_opportunity row by id. None if not found."""
        if not self._connect():
            return None
        result = strategy_module.get_opportunity_by_id(self._conn, strategy_opportunity_id)
        self._end_read_txn()
        return result

    def list_allocations(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """Return list of strategy_allocation rows (with gate_safety_name from JOIN)."""
        if not self._connect():
            return []
        result = strategy_module.list_allocations(self._conn, active_only=active_only)
        self._end_read_txn()
        return result

    def get_allocation_by_id(self, strategy_allocation_id: int) -> Optional[Dict[str, Any]]:
        """Return one strategy_allocation row by id. None if not found."""
        if not self._connect():
            return None
        result = strategy_module.get_allocation_by_id(self._conn, strategy_allocation_id)
        self._end_read_txn()
        return result

    def get_strategy_history(
        self,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        strategy_structure_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return strategy_history rows with optional filters."""
        if not self._connect():
            return []
        result = strategy_module.get_strategy_history(
            self._conn,
            from_ts=from_ts,
            to_ts=to_ts,
            strategy_structure_id=strategy_structure_id,
            limit=limit,
        )
        self._end_read_txn()
        return result

    def list_strategy_instances(
        self,
        account_id: Optional[str] = None,
        strategy_opportunity_id: Optional[int] = None,
        strategy_instance_ids: Optional[List[int]] = None,
        opened_at_from: Optional[float] = None,
        opened_at_until: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Return strategy_instance rows, optionally filtered by account_id, strategy_opportunity_id, strategy_instance_ids, opened_at range (Unix seconds)."""
        if not self._connect():
            return []
        result = strategy_instance_module.list_instances(
            self._conn,
            account_id=account_id,
            strategy_opportunity_id=strategy_opportunity_id,
            strategy_instance_ids=strategy_instance_ids,
            opened_at_from=opened_at_from,
            opened_at_until=opened_at_until,
        )
        self._end_read_txn()
        return result

    def get_strategy_instance_by_id(self, strategy_instance_id: int) -> Optional[Dict[str, Any]]:
        """Return one strategy_instance by id. None if not found."""
        if not self._connect():
            return None
        result = strategy_instance_module.get_instance_by_id(self._conn, strategy_instance_id)
        self._end_read_txn()
        return result

    def create_strategy_instance(
        self,
        strategy_opportunity_id: int,
        account_id: str,
        opened_at: Any,
        label: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Optional[int]:
        """Insert one strategy_instance. Returns strategy_instance_id or None."""
        if not self._connect():
            return None
        return strategy_instance_module.create_instance(
            self._conn,
            strategy_opportunity_id=strategy_opportunity_id,
            account_id=account_id,
            opened_at=opened_at,
            label=label,
            notes=notes,
        )

    def update_strategy_instance(
        self,
        strategy_instance_id: int,
        label: Optional[str] = None,
        notes: Optional[str] = None,
        created_at: Optional[Any] = None,
        opened_at: Optional[Any] = None,
    ) -> bool:
        """Update label, notes, created_at, and/or opened_at of a strategy instance. Returns True if updated."""
        if not self._connect():
            return False
        return strategy_instance_module.update_instance(
            self._conn, strategy_instance_id, label=label, notes=notes, created_at=created_at, opened_at=opened_at
        )

    def delete_strategy_instance(self, strategy_instance_id: int) -> bool:
        """Delete a strategy_instance by id. Returns True if deleted."""
        if not self._connect():
            return False
        return strategy_instance_module.delete_instance(self._conn, strategy_instance_id)

    def get_instance_open_option_legs(self, strategy_instance_id: int) -> list:
        """Return open OPT positions linked to a strategy instance (via executions)."""
        if not self._connect():
            return []
        result = strategy_instance_module.get_instance_open_option_legs(self._conn, strategy_instance_id)
        self._end_read_txn()
        return result

    def get_strategy_win_rate(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return ``structures`` plus ``totals_all`` (global aggregates / averages)."""
        if not self._connect():
            return {"structures": [], "totals_all": None}
        try:
            return strategy_win_rate_module.compute_win_rate_by_structure(
                self._conn, since_ts=since_ts, until_ts=until_ts
            )
        finally:
            self._end_read_txn()

    # --- Risk (delegate to status module) ---
    def get_risk_summary(self) -> Dict[str, Any]:
        if not self._connect():
            return {"daily_hedge_count": None, "daily_pnl": None, "spot": None, "symbol": None, "operations_count_24h": 0, "block_reasons": [], "ts": None}
        result = status_module.get_risk_summary(self._conn)
        self._end_read_txn()
        return result

    # --- Accounts domain (delegate to accounts module) ---
    def get_accounts_from_tables(self) -> Optional[List[Dict[str, Any]]]:
        if not self._connect():
            return None
        result = accounts_module.get_accounts_from_tables(self._conn)
        self._end_read_txn()
        return result

    def get_accounts_fetched_at(self) -> Optional[float]:
        if not self._connect():
            return None
        result = accounts_module.get_accounts_fetched_at(self._conn)
        self._end_read_txn()
        return result

    # --- Portfolio model analysis (R-M8) ---
    def get_model_analysis(self, account_id: str) -> Optional[Dict[str, Any]]:
        if not self._connect():
            return None
        from bifrost_core.portfolio.reader.portfolio_facade import get_model_analysis_for_account

        result = get_model_analysis_for_account(self._conn, account_id)
        self._end_read_txn()
        return result

    # --- Executions / transactions / performance (delegate to executions module) ---
    def get_executions(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        account_id: Optional[str] = None,
        limit: Optional[int] = 200,
        strategy_opportunity_id: Optional[int] = None,
        strategy_instance_id: Optional[int] = None,
        source_scope: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = executions_module.get_executions(
            self._conn,
            since_ts=since_ts,
            until_ts=until_ts,
            account_id=account_id,
            limit=limit,
            strategy_opportunity_id=strategy_opportunity_id,
            strategy_instance_id=strategy_instance_id,
            source_scope=source_scope,
        )
        self._end_read_txn()
        return result

    def get_executions_freshness(self) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = executions_module.get_executions_freshness(self._conn)
        self._end_read_txn()
        return result

    def get_executions_by_contract_keys(
        self,
        contract_keys: List[Tuple[str, str, str, str]],
        account_id: Optional[str] = None,
        limit: int = 5000,
    ) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = executions_module.get_executions_by_contract_keys(self._conn, contract_keys=contract_keys, account_id=account_id, limit=limit)
        self._end_read_txn()
        return result

    def get_executions_for_strategy_link(
        self,
        account_id: str,
        contract_key: Optional[str] = None,
        symbol: Optional[str] = None,
        expiry: Optional[str] = None,
        strike: Optional[Any] = None,
        option_right: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = executions_module.get_executions_for_strategy_link(
            self._conn,
            account_id=account_id,
            contract_key=contract_key,
            symbol=symbol,
            expiry=expiry,
            strike=strike,
            option_right=option_right,
            limit=limit,
        )
        self._end_read_txn()
        return result

    def get_option_stock_links(
        self,
        account_id: str,
        option_account_executions_id: int,
    ) -> Dict[str, Any]:
        if not self._connect():
            return {"links": [], "slippage_total": None, "error": "database_unavailable"}
        result = option_stock_link_module.get_option_stock_links(
            self._conn, account_id, option_account_executions_id
        )
        self._end_read_txn()
        return result

    def get_stock_link_candidates(
        self,
        account_id: str,
        option_account_executions_id: int,
        trade_date_from: Optional[str] = None,
        trade_date_to: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        if not self._connect():
            return {"executions": [], "error": "database_unavailable"}
        result = option_stock_link_module.get_stock_link_candidates(
            self._conn,
            account_id,
            option_account_executions_id,
            trade_date_from=trade_date_from,
            trade_date_to=trade_date_to,
            limit=limit,
        )
        self._end_read_txn()
        return result

    def get_option_stock_links_bulk(
        self,
        batches: List[Tuple[str, List[int]]],
    ) -> Dict[str, Any]:
        if not self._connect():
            return {"by_option_id": {}, "error": "database_unavailable"}
        result = option_stock_link_module.get_option_stock_links_bulk(self._conn, batches)
        self._end_read_txn()
        return result

    def get_executions_with_opt_pairs(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        account_id: Optional[str] = None,
        limit: int = 200,
        strategy_opportunity_id: Optional[int] = None,
        strategy_instance_id: Optional[int] = None,
        source_scope: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self._connect():
            return {"executions": [], "opt_pairs": []}
        result = executions_module.get_executions_with_opt_pairs(
            self._conn,
            since_ts=since_ts,
            until_ts=until_ts,
            account_id=account_id,
            limit=limit,
            strategy_opportunity_id=strategy_opportunity_id,
            strategy_instance_id=strategy_instance_id,
            source_scope=source_scope,
        )
        self._end_read_txn()
        return result

    def get_executions_with_opt_pairs_single_query(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        account_id: Optional[str] = None,
        limit: int = 5000,
        source_scope: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = executions_module.get_executions_with_opt_pairs_single_query(
            self._conn,
            since_ts=since_ts,
            until_ts=until_ts,
            account_id=account_id,
            limit=limit,
            source_scope=source_scope,
        )
        self._end_read_txn()
        return result

    def get_net_cash_flow(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        account_id: Optional[str] = None,
    ) -> float:
        if not self._connect():
            return 0.0
        result = executions_module.get_net_cash_flow(self._conn, since_ts=since_ts, until_ts=until_ts, account_id=account_id)
        self._end_read_txn()
        return result

    def get_transactions(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        account_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = executions_module.get_transactions(self._conn, since_ts=since_ts, until_ts=until_ts, account_id=account_id, limit=limit)
        self._end_read_txn()
        return result

    def get_performance_stats(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        account_id: Optional[str] = None,
        granularity: str = "day",
        strategy_opportunity_id: Optional[int] = None,
        strategy_instance_id: Optional[int] = None,
        source_scope: str = "performance_book",
    ) -> Dict[str, Any]:
        if not self._connect():
            return {}
        result = executions_module.get_performance_stats(
            self._conn,
            since_ts=since_ts,
            until_ts=until_ts,
            account_id=account_id,
            granularity=granularity,
            strategy_opportunity_id=strategy_opportunity_id,
            strategy_instance_id=strategy_instance_id,
            source_scope=source_scope,
        )
        self._end_read_txn()
        return result

    def get_performance_instance_summary(
        self,
        strategy_instance_id: int,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        if not self._connect():
            return executions_module._performance_response_summary_only(
                trade_count=0,
                total_realized_pnl=0.0,
                total_commission=0.0,
                net_pnl=0.0,
                win_count=0,
                loss_count=0,
            )
        try:
            return executions_module.get_performance_instance_summary_only(
                self._conn,
                strategy_instance_id=strategy_instance_id,
                since_ts=since_ts,
                until_ts=until_ts,
            )
        finally:
            self._end_read_txn()

    # --- Position×Instance attribution (delegate to executions module) ---
    def get_position_instance_attribution(
        self,
        account_id: Optional[str] = None,
        sec_type_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = executions_module.get_position_instance_attribution(
            self._conn, account_id=account_id, sec_type_filter=sec_type_filter,
        )
        self._end_read_txn()
        return result

    # --- Position categories (delegate to position_categories module) ---
    def get_position_categories(self) -> List[Dict[str, Any]]:
        if not self._connect():
            return []
        result = position_categories_module.get_position_categories(self._conn)
        self._end_read_txn()
        return result

    def create_position_category(
        self,
        name: str,
        description: Optional[str] = None,
        sort_order: Optional[int] = None,
    ) -> Tuple[Optional[int], Optional[str]]:
        if not self._connect():
            return None, "Database connection failed."
        return position_categories_module.create_position_category(
            self._conn, name=name, description=description, sort_order=sort_order
        )

    def update_position_category(
        self,
        category_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        sort_order: Optional[int] = None,
    ) -> bool:
        if not self._connect():
            return False
        return position_categories_module.update_position_category(self._conn, category_id=category_id, name=name, description=description, sort_order=sort_order)

    def delete_position_category(self, category_id: int) -> bool:
        if not self._connect():
            return False
        return position_categories_module.delete_position_category(self._conn, category_id)

    def set_position_category_tag(
        self,
        account_id: str,
        contract_key: str,
        category_id: Optional[int],
    ) -> bool:
        if not self._connect():
            return False
        return position_categories_module.set_position_category_tag(self._conn, account_id=account_id, contract_key=contract_key, category_id=category_id)

    def batch_update_execution_strategy(
        self,
        account_id: str,
        contract_key: Optional[str],
        execution_ids: Optional[list],
        strategy_opportunity_id: Optional[int],
        strategy_instance_id: Optional[int],
    ) -> int:
        """Batch update strategy attribution on account_executions (by contract_key or execution_ids). Returns updated count."""
        if not self._connect():
            return 0
        return accounts_module.batch_update_execution_strategy(
            self._conn,
            account_id=account_id,
            contract_key=contract_key,
            execution_ids=execution_ids,
            strategy_opportunity_id=strategy_opportunity_id,
            strategy_instance_id=strategy_instance_id,
        )

    def get_market_streams_symbol_order(self) -> Dict[str, Any]:
        if not self._connect():
            return {}
        result = position_categories_module.get_market_streams_symbol_order(self._conn)
        self._end_read_txn()
        return result

    def set_market_streams_symbol_order(self, category_name: str, symbols: List[str]) -> bool:
        if not self._connect():
            return False
        return position_categories_module.set_market_streams_symbol_order(self._conn, category_name=category_name, symbols=symbols)
