"""PostgreSQL implementation of StatusSink. See docs/DATABASE.md."""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2

from bifrost_core.persistence.status_sink import (
    ACCOUNTS_SNAPSHOT_KEY,
    SNAPSHOT_KEYS,
    StatusSink,
)
from bifrost_core.persistence.postgres.connection import (
    _DAEMON_LOCK_TABLES,
    _get_conn_params,
    _get_golden_source_conn_params,
    _is_lock_timeout_error,
    release_pg_locks_for_tables,
)
from bifrost_core.persistence.postgres.ddl import _ensure_tables
from bifrost_core.persistence.postgres.accounts_sync import (
    _has_meaningful_commission,
    sync_accounts_snapshot_to_tables,
)
from bifrost_core.persistence.postgres.brokerage_tables import (
    COMMISSIONS,
    CONTRACT_QUOTE_LIVE,
    EXECUTIONS_RAW_TWS,
    OPEN_ORDERS,
    POSITIONS,
)
from bifrost_core.persistence import redis_daemon_state as rds

logger = logging.getLogger(__name__)


class PostgreSQLSink(StatusSink):
    """Daemon IPC state in Redis; PG still used for strategy_history / brokerage / settings."""

    def __init__(self, config: dict):
        self._config = config
        self._conn: Optional[Any] = None
        self._golden_conn: Optional[Any] = None
        self._redis: Optional[Any] = None
        self._connect()
        self._connect_golden()
        self._connect_redis()

    def _connect_redis(self) -> None:
        self._redis = rds.connect_daemon_state_redis(self._config)
        if self._redis is not None:
            logger.info("Daemon state Redis connected (trading IPC)")
            # Default suspended=true until explicit Resume (matches former PG seed).
            state = rds.read_trading_daemon_state(self._redis) or {}
            if "suspended" not in state:
                rds.set_trading_run_status(
                    self._redis, suspended=True, heartbeat_interval_sec=10
                )
        else:
            logger.warning("Daemon state Redis unavailable — IPC writes will no-op")

    def _ensure_redis(self) -> bool:
        if self._redis is None:
            self._connect_redis()
        if self._redis is None:
            return False
        try:
            self._redis.ping()
            return True
        except Exception:
            self._redis = None
            self._connect_redis()
            return self._redis is not None

    def _connect(self) -> None:
        params = _get_conn_params(self._config)
        for attempt in (1, 2):
            try:
                self._conn = psycopg2.connect(**params)
                # Avoid blocking forever if another session holds a lock on shared PG tables.
                with self._conn.cursor() as cur:
                    cur.execute("SET lock_timeout = '5s'")
                    cur.execute("SET idle_in_transaction_session_timeout = '60s'")
                self._conn.commit()
                _ensure_tables(self._conn)
                logger.info(
                    "PostgreSQL sink connected: %s@%s:%s/%s",
                    params["user"],
                    params["host"],
                    params["port"],
                    params["dbname"],
                )
                return
            except Exception as e:
                self._conn = None
                if attempt == 1 and _is_lock_timeout_error(e):
                    n = release_pg_locks_for_tables(self._config)
                    if n > 0:
                        logger.info(
                            "Released %s backend(s) holding lock on %s; retrying connect",
                            n,
                            _DAEMON_LOCK_TABLES,
                        )
                        time.sleep(0.5)
                        continue
                logger.warning("PostgreSQL sink connect failed: %s", e)
                return

    def _connect_golden(self) -> None:
        """Connect to bifrost_golden_source for brokerage.* writes."""
        params = _get_golden_source_conn_params(self._config)
        try:
            self._golden_conn = psycopg2.connect(**{**params, "connect_timeout": 10})
            with self._golden_conn.cursor() as cur:
                cur.execute("SET lock_timeout = '5s'")
                cur.execute("SET idle_in_transaction_session_timeout = '60s'")
            self._golden_conn.commit()
            try:
                from bifrost_core.persistence.postgres.brokerage_ddl import (
                    ensure_brokerage_schema,
                )

                ensure_brokerage_schema(self._golden_conn)
                self._golden_conn.commit()
            except Exception as ddl_err:
                try:
                    self._golden_conn.rollback()
                except Exception:
                    pass
                logger.debug("ensure_brokerage_schema (best-effort): %s", ddl_err)
            logger.info(
                "PostgreSQL golden_source connected: %s@%s:%s/%s",
                params["user"],
                params["host"],
                params["port"],
                params["dbname"],
            )
        except Exception as e:
            self._golden_conn = None
            logger.warning("PostgreSQL golden_source connect failed: %s", e)

    def _ensure_conn(self) -> bool:
        if self._conn is None:
            self._connect()
        if self._conn is not None:
            try:
                self._conn.rollback()
                return True
            except Exception:
                self._conn = None
                self._connect()
        return self._conn is not None

    def _ensure_golden_conn(self) -> bool:
        if self._golden_conn is None:
            self._connect_golden()
        if self._golden_conn is not None:
            try:
                self._golden_conn.rollback()
                return True
            except Exception:
                self._golden_conn = None
                self._connect_golden()
        return self._golden_conn is not None

    def write_snapshot(
        self, snapshot: Dict[str, Any], append_history: bool = False
    ) -> None:
        """Write trading status snapshot to Redis HASH; optionally append strategy_history in PG."""
        keys = tuple(SNAPSHOT_KEYS)
        fields = {k: snapshot.get(k) for k in keys}
        if self._ensure_redis():
            rds.write_trading_daemon_state(self._redis, fields)

        raw_accounts = (
            snapshot.get(ACCOUNTS_SNAPSHOT_KEY)
            if ACCOUNTS_SNAPSHOT_KEY in snapshot
            else None
        )
        # strategy_history + accounts still need PG
        if not append_history and not (
            isinstance(raw_accounts, list)
            and raw_accounts
            and os.environ.get("ACCOUNT_SYNC_DAEMON_ENABLED", "").strip().lower()
            not in ("1", "true", "yes")
        ):
            return
        if not self._ensure_conn():
            return
        try:
            with self._conn.cursor() as cur:
                if append_history:
                    cur.execute(
                        "SELECT active_strategy_structure_id FROM settings WHERE id = 1"
                    )
                    set_row = cur.fetchone()
                    structure_id = set_row[0] if set_row and set_row[0] is not None else None
                    ts_val = snapshot.get("ts")
                    if ts_val is None:
                        ts_val = time.time()
                    state_summary = {
                        k: snapshot.get(k)
                        for k in (
                            "daemon_state",
                            "trading_state",
                            "symbol",
                            "net_delta",
                            "daily_hedge_count",
                            "daily_pnl",
                            "config_summary",
                        )
                    }
                    cur.execute(
                        """
                        INSERT INTO strategy_history (strategy_structure_id, ts, state_summary, created_at)
                        VALUES (%s, to_timestamp(%s), %s::jsonb, now())
                        """,
                        (structure_id, ts_val, json.dumps(state_summary)),
                    )
            if isinstance(raw_accounts, list) and raw_accounts:
                if os.environ.get("ACCOUNT_SYNC_DAEMON_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
                    if self._ensure_golden_conn():
                        sync_accounts_snapshot_to_tables(self._golden_conn, raw_accounts)
                        self._golden_conn.commit()
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            if self._golden_conn is not None:
                try:
                    self._golden_conn.rollback()
                except Exception:
                    pass
            logger.warning("PostgreSQL write_snapshot (history/accounts) failed: %s", e, exc_info=True)

    def sync_accounts_only(self, accounts_list: Optional[List[Dict[str, Any]]]) -> None:
        """R-A1 / Secondary: write only the given accounts to brokerage.account + brokerage.positions.
        Used by Secondary position callback to push listener_connector_2 data without full snapshot."""
        if not accounts_list or not isinstance(accounts_list, list):
            return
        if not self._ensure_golden_conn():
            return
        try:
            sync_accounts_snapshot_to_tables(self._golden_conn, accounts_list)
            self._golden_conn.commit()
        except Exception as e:
            self._golden_conn.rollback()
            logger.warning("PostgreSQL sync_accounts_only failed: %s", e, exc_info=True)

    def write_operation(self, record: Dict[str, Any]) -> None:
        """No-op: daemon_auto_operations retired (Wave 1)."""
        return

    def write_contract_quote_live(self, rows):
        """R-M6: 写入 brokerage.contract_quote_live（按 contract_key upsert）。rows: Iterable[Dict]。
        过滤 NaN/Null：价格字段若为 NaN、inf 或空则写入 NULL，不污染数据库。若整行无有效价格则跳过该行。"""
        if not rows:
            return
        if not self._ensure_golden_conn():
            return
        logger.info("[R-M6] write_contract_quote_live: %s rows received", len(rows))

        def _sanitize(v):
            if v is None:
                return None
            try:
                f = float(v)
                return f if math.isfinite(f) else None
            except (TypeError, ValueError):
                return None

        try:
            with self._golden_conn.cursor() as cur:
                for r in rows:
                    contract_key = r.get("contract_key")
                    if not contract_key:
                        logger.warning(
                            "[R-M6] write_contract_quote_live: missing contract_key in row: %s",
                            r,
                        )
                        continue
                    last = _sanitize(r.get("last"))
                    bid = _sanitize(r.get("bid"))
                    ask = _sanitize(r.get("ask"))
                    mid = _sanitize(r.get("mid"))
                    if last is None and bid is None and ask is None and mid is None:
                        logger.debug(
                            "[R-M6] write_contract_quote_live: skip row (all price fields NaN/Null): %s",
                            contract_key,
                        )
                        continue
                    cur.execute(
                        f"""
                        INSERT INTO {CONTRACT_QUOTE_LIVE} (
                            contract_key, symbol, sec_type, expiry, strike, option_right,
                            last, bid, ask, mid, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (contract_key) DO UPDATE SET
                            symbol = EXCLUDED.symbol,
                            sec_type = EXCLUDED.sec_type,
                            expiry = EXCLUDED.expiry,
                            strike = EXCLUDED.strike,
                            option_right = EXCLUDED.option_right,
                            last = EXCLUDED.last,
                            bid = EXCLUDED.bid,
                            ask = EXCLUDED.ask,
                            mid = EXCLUDED.mid,
                            updated_at = now()
                        """,
                        (
                            contract_key,
                            r.get("symbol"),
                            r.get("sec_type"),
                            r.get("expiry"),
                            r.get("strike"),
                            r.get("option_right"),
                            last,
                            bid,
                            ask,
                            mid,
                        ),
                    )
            self._golden_conn.commit()
            logger.info("[R-M6] write_contract_quote_live: commit ok")
        except Exception as e:
            self._golden_conn.rollback()
            logger.warning("write_contract_quote_live failed: %s", e, exc_info=True)

    def write_account_executions(self, rows: Any) -> None:
        """R-A2: write executions to brokerage.executions_raw_tws; commissions to brokerage.commissions."""
        if not rows:
            return
        if not self._ensure_golden_conn():
            return
        try:
            import json
            with self._golden_conn.cursor() as cur:
                for r in rows:
                    exec_id = r.get("exec_id")
                    account_id = r.get("account_id")
                    exec_time = r.get("time")
                    symbol = r.get("symbol")
                    sec_type = r.get("sec_type")
                    side = r.get("side")
                    quantity = r.get("quantity")
                    price = r.get("price")
                    source = r.get("source")
                    expiry = r.get("expiry")
                    strike = r.get("strike")
                    option_right = r.get("option_right")
                    exchange = r.get("exchange")
                    order_id = r.get("order_id")
                    cum_qty = r.get("cum_qty")
                    contract_key = r.get("contract_key")
                    currency = r.get("currency")
                    asset_category = r.get("asset_category")
                    sub_category = r.get("sub_category")
                    description = r.get("description")
                    conid = r.get("conid")
                    security_id = r.get("security_id")
                    security_id_type = r.get("security_id_type")
                    cusip = r.get("cusip")
                    isin = r.get("isin")
                    figi = r.get("figi")
                    listing_exchange = r.get("listing_exchange")
                    underlying_conid = r.get("underlying_conid")
                    underlying_symbol = r.get("underlying_symbol")
                    underlying_security_id = r.get("underlying_security_id")
                    underlying_listing_exchange = r.get("underlying_listing_exchange")
                    issuer = r.get("issuer")
                    issuer_country_code = r.get("issuer_country_code")
                    trade_id = r.get("trade_id")
                    related_trade_id = r.get("related_trade_id")
                    report_date = r.get("report_date")
                    trade_date = r.get("trade_date")
                    settle_date_target = r.get("settle_date_target")
                    transaction_type = r.get("transaction_type")
                    multiplier = r.get("multiplier")
                    principal_adjust_factor = r.get("principal_adjust_factor")
                    proceeds = r.get("proceeds")
                    taxes = r.get("taxes")
                    net_cash = r.get("net_cash")
                    close_price = r.get("close_price")
                    open_close_indicator = r.get("open_close_indicator")
                    notes = r.get("notes")
                    cost = r.get("cost")
                    fifo_pnl_realized = r.get("fifo_pnl_realized")
                    mtm_pnl = r.get("mtm_pnl")
                    trade_money = r.get("trade_money")
                    fx_rate_to_base = r.get("fx_rate_to_base")
                    acct_alias = r.get("acct_alias")
                    model = r.get("model")
                    raw_extra = r.get("raw_extra")
                    if raw_extra is not None and not isinstance(raw_extra, str):
                        raw_extra = json.dumps(raw_extra) if raw_extra else None

                    sec_type_norm = (sec_type or "").strip().upper()
                    if sec_type_norm == "OPT":
                        sym_key = (symbol or "").strip()
                        exp_val = expiry
                        if isinstance(exp_val, (int, float)) and math.isfinite(exp_val):
                            exp_key = str(int(exp_val))
                        else:
                            exp_key = (exp_val or "").strip().replace("-", "")
                        strike_raw = strike
                        try:
                            strike_key = float(strike_raw) if strike_raw not in ("", None) else None
                        except (TypeError, ValueError):
                            strike_key = None
                        right_key = (option_right or "").strip().upper()
                        if len(right_key) > 1:
                            right_key = "C" if right_key.startswith("C") else "P" if right_key.startswith("P") else right_key[:1]

                        source_norm = (source or "").strip()
                        if (
                            source_norm in ("tws_event", "tws_client")
                            and sym_key
                            and exp_key
                            and strike_key is not None
                            and right_key
                        ):
                            exp_digits = "".join(ch for ch in exp_key if ch.isdigit())
                            yymmdd = exp_digits[2:8] if len(exp_digits) >= 8 else exp_digits[-6:]
                            try:
                                strike_int = int(round(strike_key * 1000.0))
                            except (TypeError, ValueError, OverflowError):
                                strike_int = None
                            if yymmdd and strike_int is not None:
                                strike_8 = f"{strike_int:08d}"
                                local_symbol = f"{sym_key}  {yymmdd}{right_key}{strike_8}"
                                contract_key = "|".join(
                                    [
                                        local_symbol,
                                        "OPT",
                                        exp_key,
                                        str(strike_key),
                                        right_key,
                                    ]
                                )
                        if not contract_key and sym_key:
                            contract_key = "|".join(
                                [
                                    sym_key,
                                    "OPT",
                                    exp_key,
                                    str(strike_key) if strike_key is not None else "",
                                    right_key,
                                ]
                            )
                    if exec_time is not None:
                        try:
                            from datetime import datetime, timezone
                            if isinstance(exec_time, (int, float)):
                                exec_dt = datetime.fromtimestamp(exec_time, tz=timezone.utc)
                            else:
                                exec_dt = exec_time
                        except Exception:
                            exec_dt = None
                    else:
                        exec_dt = None
                    if (source or "").strip() != "flex_trades" and trade_date is None and exec_dt is not None:
                        try:
                            trade_date = exec_dt.date() if hasattr(exec_dt, "date") else None
                        except Exception:
                            trade_date = None
                    cols = (
                        "account_id, exec_id, exec_time, symbol, sec_type, side, quantity, price, source, "
                        "expiry, strike, option_right, exchange, order_id, cum_qty, contract_key, "
                        "asset_category, sub_category, description, conid, security_id, security_id_type, "
                        "cusip, isin, figi, listing_exchange, underlying_conid, underlying_symbol, "
                        "underlying_security_id, underlying_listing_exchange, issuer, issuer_country_code, "
                        "trade_id, related_trade_id, report_date, trade_date, settle_date_target, "
                        "transaction_type, multiplier, principal_adjust_factor, proceeds, taxes, net_cash, "
                        "close_price, open_close_indicator, notes, cost, fifo_pnl_realized, mtm_pnl, "
                        "trade_money, fx_rate_to_base, acct_alias, model, raw_extra"
                    )
                    placeholders = ", ".join(["%s"] * 54)
                    vals = (
                        account_id,
                        exec_id,
                        exec_dt,
                        symbol,
                        sec_type,
                        side,
                        quantity,
                        price,
                        source,
                        expiry,
                        strike,
                        option_right,
                        exchange,
                        order_id,
                        cum_qty,
                        contract_key,
                        asset_category,
                        sub_category,
                        description,
                        conid,
                        security_id,
                        security_id_type,
                        cusip,
                        isin,
                        figi,
                        listing_exchange,
                        underlying_conid,
                        underlying_symbol,
                        underlying_security_id,
                        underlying_listing_exchange,
                        issuer,
                        issuer_country_code,
                        trade_id,
                        related_trade_id,
                        report_date,
                        trade_date,
                        settle_date_target,
                        transaction_type,
                        multiplier,
                        principal_adjust_factor,
                        proceeds,
                        taxes,
                        net_cash,
                        close_price,
                        open_close_indicator,
                        notes,
                        cost,
                        fifo_pnl_realized,
                        mtm_pnl,
                        trade_money,
                        fx_rate_to_base,
                        acct_alias,
                        model,
                        raw_extra,
                    )
                    # Write: brokerage.executions_raw_tws
                    try:
                        if exec_id:
                            cur.execute(
                                f"""
                                INSERT INTO {EXECUTIONS_RAW_TWS} ({cols})
                                VALUES ({placeholders})
                                ON CONFLICT (exec_id) WHERE exec_id IS NOT NULL AND exec_id != '' DO NOTHING
                                """,
                                vals,
                            )
                        else:
                            cur.execute(
                                f"""
                                INSERT INTO {EXECUTIONS_RAW_TWS} ({cols})
                                VALUES ({placeholders})
                                """,
                                vals,
                            )
                    except Exception:
                        pass  # raw table may not exist yet on older DBs

                    commission = r.get("commission")
                    realized_pnl = r.get("realized_pnl")
                    currency = r.get("currency")
                    yield_ = r.get("yield_")
                    yield_redemption_date = r.get("yield_redemption_date")

                    def _null_if_zero(v):
                        if v is None:
                            return None
                        try:
                            if float(v) == 0:
                                return None
                        except (TypeError, ValueError):
                            pass
                        return v if (v != "" or v is None) else None

                    commission_val = _null_if_zero(commission)
                    realized_pnl_val = _null_if_zero(realized_pnl)
                    yield_val = _null_if_zero(yield_)
                    yield_redemption_date_val = _null_if_zero(yield_redemption_date)
                    currency_val = currency if (currency and str(currency).strip()) else None

                    has_comm = (
                        _has_meaningful_commission(commission)
                        or _has_meaningful_commission(realized_pnl)
                        or _has_meaningful_commission(currency, is_numeric=False)
                        or _has_meaningful_commission(yield_)
                        or _has_meaningful_commission(yield_redemption_date)
                    )
                    if exec_id and has_comm:
                        cur.execute(
                            f"""
                            INSERT INTO {COMMISSIONS} (exec_id, commission, currency, realized_pnl, yield_, yield_redemption_date)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (exec_id) DO UPDATE SET
                                commission = CASE
                                    WHEN EXCLUDED.commission IS NOT NULL AND EXCLUDED.commission != 0 THEN EXCLUDED.commission
                                    ELSE {COMMISSIONS}.commission
                                END,
                                currency = CASE
                                    WHEN EXCLUDED.currency IS NOT NULL AND TRIM(COALESCE(EXCLUDED.currency, '')) != '' THEN EXCLUDED.currency
                                    ELSE {COMMISSIONS}.currency
                                END,
                                realized_pnl = CASE
                                    WHEN EXCLUDED.realized_pnl IS NOT NULL AND EXCLUDED.realized_pnl != 0 THEN EXCLUDED.realized_pnl
                                    ELSE {COMMISSIONS}.realized_pnl
                                END,
                                yield_ = CASE
                                    WHEN EXCLUDED.yield_ IS NOT NULL AND EXCLUDED.yield_ != 0 THEN EXCLUDED.yield_
                                    ELSE {COMMISSIONS}.yield_
                                END,
                                yield_redemption_date = CASE
                                    WHEN EXCLUDED.yield_redemption_date IS NOT NULL AND EXCLUDED.yield_redemption_date != 0 THEN EXCLUDED.yield_redemption_date
                                    ELSE {COMMISSIONS}.yield_redemption_date
                                END
                            """,
                            (exec_id, commission_val, currency_val, realized_pnl_val, yield_val, yield_redemption_date_val),
                        )
            self._golden_conn.commit()
            logger.info("[R-A2] write_account_executions: wrote %s rows", len(rows))
        except Exception as e:
            self._golden_conn.rollback()
            logger.warning("write_account_executions failed: %s", e, exc_info=True)

    def update_execution_commission(
        self, exec_id: str, commission: Any, realized_pnl: Any, currency: Any,
        yield_: Any = None, yield_redemption_date: Any = None,
    ) -> None:
        """R-A2: 收到 commissionReport 事件时按 exec_id 写入 brokerage.commissions。"""
        if not exec_id:
            return
        if not self._ensure_golden_conn():
            return
        def _nz(v):
            if v is None:
                return None
            try:
                if float(v) == 0:
                    return None
            except (TypeError, ValueError):
                pass
            return v
        commission_val = _nz(commission)
        realized_pnl_val = _nz(realized_pnl)
        yield_val = _nz(yield_)
        yield_redemption_date_val = _nz(yield_redemption_date)
        currency_val = currency if (currency and str(currency).strip()) else None
        try:
            with self._golden_conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {COMMISSIONS} (exec_id, commission, currency, realized_pnl, yield_, yield_redemption_date)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (exec_id) DO UPDATE SET
                        commission = CASE
                            WHEN EXCLUDED.commission IS NOT NULL AND EXCLUDED.commission != 0 THEN EXCLUDED.commission
                            ELSE {COMMISSIONS}.commission
                        END,
                        currency = CASE
                            WHEN EXCLUDED.currency IS NOT NULL AND TRIM(COALESCE(EXCLUDED.currency, '')) != '' THEN EXCLUDED.currency
                            ELSE {COMMISSIONS}.currency
                        END,
                        realized_pnl = CASE
                            WHEN EXCLUDED.realized_pnl IS NOT NULL AND EXCLUDED.realized_pnl != 0 THEN EXCLUDED.realized_pnl
                            ELSE {COMMISSIONS}.realized_pnl
                        END,
                        yield_ = CASE
                            WHEN EXCLUDED.yield_ IS NOT NULL AND EXCLUDED.yield_ != 0 THEN EXCLUDED.yield_
                            ELSE {COMMISSIONS}.yield_
                        END,
                        yield_redemption_date = CASE
                            WHEN EXCLUDED.yield_redemption_date IS NOT NULL AND EXCLUDED.yield_redemption_date != 0 THEN EXCLUDED.yield_redemption_date
                            ELSE {COMMISSIONS}.yield_redemption_date
                        END
                    """,
                    (exec_id, commission_val, currency_val, realized_pnl_val, yield_val, yield_redemption_date_val),
                )
            self._golden_conn.commit()
        except Exception as e:
            self._golden_conn.rollback()
            logger.warning("update_execution_commission failed: exec_id=%r %s", exec_id, e)

    def write_open_orders(self, orders: List[Dict[str, Any]]) -> None:
        """R-A5: 写入当前未成交订单快照到 brokerage.open_orders；全量替换（TRUNCATE + INSERT）。"""
        if not self._ensure_golden_conn():
            return
        try:
            with self._golden_conn.cursor() as cur:
                cur.execute(f"TRUNCATE TABLE {OPEN_ORDERS}")
                if orders:
                    for o in orders:
                        cur.execute(
                            f"""
                            INSERT INTO {OPEN_ORDERS}
                            (order_id, perm_id, account_id, symbol, sec_type, action, total_quantity,
                             filled, remaining, limit_price, status, contract_key, updated_ts)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                            """,
                            (
                                o.get("order_id"),
                                o.get("perm_id"),
                                o.get("account_id"),
                                o.get("symbol"),
                                o.get("sec_type"),
                                o.get("action"),
                                o.get("total_quantity"),
                                o.get("filled"),
                                o.get("remaining"),
                                o.get("limit_price"),
                                o.get("status"),
                                o.get("contract_key"),
                            ),
                        )
            self._golden_conn.commit()
        except Exception as e:
            self._golden_conn.rollback()
            logger.warning("write_open_orders failed: %s", e, exc_info=True)

    def write_ohlc_bars(self, rows: Any) -> None:
        """Write stock OHLC bars via Plugin Market Data API (POST /stocks/bars/ingest).

        Delegates to the same HTTP client used by monitor/reader/market.py.
        StatusSink's PG connection is preserved for daemon_*/account_* writes.
        Failure logs a warning but does not crash the daemon.
        """
        if not rows:
            return
        from bifrost_core.monitor.market_write_client import post_bars_ingest

        payload = []
        for r in rows:
            symbol = (r.get("symbol") or "").strip()
            period = (r.get("period") or "1 D").strip()
            bar_time = r.get("bar_time")
            if bar_time is None or not symbol:
                continue
            if isinstance(bar_time, (int, float)):
                bar_dt = datetime.fromtimestamp(float(bar_time), tz=timezone.utc)
            else:
                bar_dt = bar_time
            if period.upper() == "1 D":
                bar_date_str = r.get("bar_date")
                if bar_date_str:
                    bt_iso = str(bar_date_str)[:10]
                elif isinstance(bar_dt, datetime):
                    bt_iso = bar_dt.strftime("%Y-%m-%d")
                else:
                    bt_iso = str(bar_dt)
            else:
                bt_iso = bar_dt.isoformat() if isinstance(bar_dt, datetime) else str(bar_dt)
            payload.append({
                "symbol": symbol,
                "period": period,
                "bar_time": bt_iso,
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r.get("close"),
                "volume": r.get("volume"),
            })
        if not payload:
            return
        try:
            resp = post_bars_ingest(payload)
            logger.info(
                "[R-A3] write_ohlc_bars: wrote %s rows via Plugin API",
                resp.get("written", len(payload)),
            )
        except Exception as e:
            logger.warning("write_ohlc_bars failed: %s", e, exc_info=True)

    # Control commands older than this are ignored (consumed but not executed).
    CONTROL_CMD_MAX_AGE_SEC = 60

    def poll_and_consume_control(
        self,
        consume_only: Optional[tuple] = None,
    ) -> Optional[str]:
        """Poll Redis STREAM control command; return command or None."""
        if not self._ensure_redis():
            return None
        cmd = rds.consume_trading_control(self._redis, block_ms=0)
        if not cmd:
            return None
        cmd = cmd.strip().lower()
        if cmd not in (
            "stop",
            "flatten",
            "retry_ib",
            "release_ib",
            "refresh_accounts",
            "refresh_replay",
            "refresh_ticker_subscriptions",
            "release_ticker_subscriptions",
            "init_ticker_subscriptions",
        ):
            cmd = "stop"
        if consume_only is not None and cmd not in consume_only:
            # Re-publish so another poller can pick it up (rare path).
            rds.publish_trading_control(self._redis, cmd, source="requeue")
            return None
        logger.info("Consumed control command from Redis stream: %s", cmd)
        return cmd

    def write_daemon_heartbeat(
        self,
        hedge_running: bool,
        ib_connected: bool = False,
        ib_client_id: Optional[int] = None,
        next_retry_ts: Optional[float] = None,
        seconds_until_retry: Optional[int] = None,
        heartbeat_interval_sec: Optional[float] = None,
        redis_quotes_connected: bool = False,
        listener_connected: bool = False,
        listener_client_id: Optional[int] = None,
        listener_2_connected: bool = False,
        listener_2_client_id: Optional[int] = None,
        mock_hedging: bool = True,
    ) -> None:
        """Write trading daemon heartbeat fields to Redis state HASH."""
        if not self._ensure_redis():
            return
        iv = int(heartbeat_interval_sec) if heartbeat_interval_sec is not None else None
        rds.write_trading_daemon_state(
            self._redis,
            {
                "last_ts": time.time(),
                "hedge_running": hedge_running,
                "ib_connected": ib_connected,
                "ib_client_id": ib_client_id if ib_client_id is not None else "",
                "graceful_shutdown_at": "",
                "heartbeat_interval_sec": iv,
                "redis_quotes_connected": redis_quotes_connected,
                "mock_hedging": mock_hedging,
            },
        )

    def write_daemon_control_message(self, message: Optional[str]) -> None:
        """Set or clear last_control_message on Redis state HASH."""
        if not self._ensure_redis():
            return
        rds.write_trading_daemon_state(
            self._redis, {"last_control_message": message or ""}
        )

    def write_daemon_subscribed_tickers(self, symbols: List[str]) -> None:
        """Write subscribed_tickers list to Redis state HASH."""
        if not self._ensure_redis():
            return
        rds.write_trading_daemon_state(
            self._redis, {"subscribed_tickers": symbols or []}
        )

    def get_last_ib_client_id(self) -> Optional[int]:
        """Read ib_client_id from Redis trading state."""
        if not self._ensure_redis():
            return None
        state = rds.read_trading_daemon_state(self._redis)
        if not state:
            return None
        v = state.get("ib_client_id")
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def get_ib_connection_config(self) -> Optional[Dict[str, Any]]:
        """Read settings.ib_host_account_id for R-A4 (hedging / market data account). Host/port/client IDs come from config YAML."""
        if not self._ensure_conn():
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT ib_host_account_id FROM settings WHERE id = 1")
                row = cur.fetchone()
            self._conn.rollback()
            if row is None:
                return None
            if row[0] is None or not str(row[0]).strip():
                return None
            return {"host_account_id": str(row[0]).strip()}
        except Exception as e:
            self._conn.rollback()
            logger.debug("get_ib_connection_config failed: %s", e)
            return None

    def get_watchlist_stk_symbols(self) -> List[str]:
        """Return distinct symbol strings from watchlist where sec_type is STK (or null/empty).
        Used by daemon to subscribe to market data for Watchlist stocks only (R-RM*)."""
        if not self._ensure_conn():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT TRIM(symbol) AS sym FROM watchlist
                    WHERE symbol IS NOT NULL AND TRIM(symbol) != ''
                    AND (sec_type IS NULL OR UPPER(TRIM(sec_type)) = 'STK')
                    ORDER BY sym
                    """
                )
                rows = cur.fetchall()
            self._conn.rollback()
            return [str(r[0]) for r in rows if r and r[0]]
        except Exception as e:
            logger.debug("get_watchlist_stk_symbols failed: %s", e)
            self._conn.rollback()
            return []

    def get_watchlist_opt_contracts(self) -> List[Dict[str, Any]]:
        """Return watchlist rows where sec_type is OPT (contract_key, symbol, sec_type, expiry, strike, option_right).
        Used by daemon to subscribe to Real-time ticker for Watchlist options. Ordered by created_at DESC for consistent truncation."""
        if not self._ensure_conn():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT contract_key, symbol, sec_type, expiry, strike, option_right
                    FROM watchlist
                    WHERE sec_type IS NOT NULL AND UPPER(TRIM(sec_type)) = 'OPT'
                    ORDER BY created_at DESC NULLS LAST
                    """
                )
                rows = cur.fetchall()
            self._conn.rollback()
            return [
                {
                    "contract_key": str(r[0]),
                    "symbol": str(r[1]) if r[1] else "",
                    "sec_type": str(r[2]) if r[2] else "OPT",
                    "expiry": str(r[3]) if r[3] else "",
                    "strike": float(r[4]) if r[4] is not None else None,
                    "option_right": str(r[5]) if r[5] else "",
                }
                for r in rows
                if r and r[0]
            ]
        except Exception as e:
            logger.debug("get_watchlist_opt_contracts failed: %s", e)
            self._conn.rollback()
            return []

    def get_contract_quotes(self, contract_keys: List[str]) -> List[Dict[str, Any]]:
        """Return bid/ask/last/mid from brokerage.contract_quote_live (via per-env FDW) for given contract_keys."""
        if not contract_keys or not self._ensure_conn():
            return []
        keys = [k for k in contract_keys if k and str(k).strip()]
        if not keys:
            return []
        try:
            with self._conn.cursor() as cur:
                placeholders = ", ".join("%s" for _ in keys)
                cur.execute(
                    f"""
                    SELECT contract_key, symbol, sec_type, expiry, strike, option_right, bid, ask, last, mid
                    FROM {CONTRACT_QUOTE_LIVE}
                    WHERE contract_key IN (""" + placeholders + """)
                    """,
                    tuple(keys),
                )
                rows = cur.fetchall()
            self._conn.rollback()
            return [
                {
                    "contract_key": r[0],
                    "symbol": r[1],
                    "sec_type": r[2],
                    "expiry": r[3],
                    "strike": r[4],
                    "option_right": r[5],
                    "bid": float(r[6]) if r[6] is not None else None,
                    "ask": float(r[7]) if r[7] is not None else None,
                    "last": float(r[8]) if r[8] is not None else None,
                    "mid": float(r[9]) if r[9] is not None else None,
                }
                for r in rows
                if r
            ]
        except Exception as e:
            logger.debug("get_contract_quotes failed: %s", e)
            self._conn.rollback()
            return []

    def get_stream_position_stk_symbols(self) -> List[str]:
        """Return distinct STK symbols from brokerage.positions for stream host/secondary accounts.
        JOINs settings on per-env conn (FDW-qualified positions)."""
        if not self._ensure_conn():
            return []
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT stream_host_account_id, stream_secondary_account_id FROM settings WHERE id = 1"
                )
                row = cur.fetchone()
            if not row:
                return []
            account_ids: List[str] = []
            for i in (0, 1):
                v = row[i] if i < len(row) and row[i] is not None else None
                if v is not None and str(v).strip():
                    account_ids.append(str(v).strip())
            if not account_ids:
                return []
            placeholders = ", ".join("%s" for _ in account_ids)
            with self._conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT DISTINCT TRIM(ap.symbol) AS sym
                    FROM {POSITIONS} ap
                    WHERE ap.account_id IN (""" + placeholders + """)
                    AND ap.symbol IS NOT NULL AND TRIM(ap.symbol) != ''
                    AND (ap.sec_type IS NULL OR UPPER(TRIM(ap.sec_type)) = 'STK')
                    AND COALESCE(ap.position, 0) != 0
                    ORDER BY sym
                    """,
                    tuple(account_ids),
                )
                rows = cur.fetchall()
            self._conn.rollback()
            return [str(r[0]) for r in rows if r and r[0]]
        except Exception as e:
            logger.debug("get_stream_position_stk_symbols failed: %s", e)
            self._conn.rollback()
            return []

    def write_daemon_graceful_shutdown(self) -> None:
        """Mark graceful shutdown on Redis trading state (for monitoring)."""
        if not self._ensure_redis():
            return
        ok = rds.write_trading_daemon_state(
            self._redis,
            {
                "graceful_shutdown_at": time.time(),
                "last_ts": time.time(),
                "ib_client_id": "",
                "hedge_running": False,
            },
        )
        if ok:
            logger.info(
                "Wrote trading daemon graceful_shutdown_at and cleared ib_client_id (Redis)"
            )

    def poll_run_status(self) -> tuple[bool, Optional[float]]:
        """Read suspended / heartbeat_interval_sec from Redis trading state.
        Default when missing: suspended=True so Daemon does not hedge until explicit Resume."""
        if not self._ensure_redis():
            logger.debug("poll_run_status: no Redis → suspended=True, interval=None")
            return True, None
        state = rds.read_trading_daemon_state(self._redis)
        if not state:
            logger.debug("poll_run_status: empty state → suspended=True, interval=None")
            return True, None
        suspended = bool(state.get("suspended", True)) if "suspended" in state else True
        interval = state.get("heartbeat_interval_sec")
        try:
            interval_f = float(interval) if interval is not None else None
        except (TypeError, ValueError):
            interval_f = None
        logger.debug(
            "poll_run_status: Redis → suspended=%s, interval=%s", suspended, interval_f
        )
        return suspended, interval_f

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._golden_conn:
            try:
                self._golden_conn.close()
            except Exception:
                pass
            self._golden_conn = None
        self._redis = None
