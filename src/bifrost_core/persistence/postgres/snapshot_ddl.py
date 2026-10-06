"""Daily book snapshots: positions per trade and account NAV (W4, core 0.48.0).

Two forward-only tables. Nothing else in the system keeps yesterday's book: the broker tables
(``brokerage.positions`` / ``brokerage.account``) hold the current state only, so a day that is
not captured is lost for good. A nightly job (``bifrost_core.portfolio.snapshot``) writes them;
nothing else does.

* ``position_snapshot_daily`` -- one row per session, account, contract and trade (a position
  split across trades is one row per trade; the part no trade explains is a ``trade_id IS NULL``
  row). ``trade_id`` has no FK: a snapshot is history and must not stop a trade from being
  deleted.
* ``account_nav_daily`` -- one row per session and account: net liquidation, cash, buying power.
  The start-of-range balance that time-weighted return and Sharpe need. Core 0.51.0 adds the
  margin-pressure history (SNAPSHOT-SPEC 1.1): ``cushion``, ``excess_liquidity``,
  ``maint_margin_req`` from IB's account summary; rows written before it keep them NULL.

Additive and idempotent: ``CREATE ... IF NOT EXISTS`` and ``ADD COLUMN IF NOT EXISTS`` only, no
change to any existing object or column.
The runtime role ``trade_app_<env>`` reads and writes them through bifrost's default
privileges in ``public`` (TD-85).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

POSITION_SNAPSHOT_DAILY = "position_snapshot_daily"
ACCOUNT_NAV_DAILY = "account_nav_daily"

SNAPSHOT_DDL: tuple[str, ...] = (
    f"""
    CREATE TABLE IF NOT EXISTS {POSITION_SNAPSHOT_DAILY} (
        position_snapshot_daily_id bigserial PRIMARY KEY,
        snapshot_date date NOT NULL,
        account_id text NOT NULL,
        contract_key text NOT NULL,
        trade_id bigint,
        symbol text,
        sec_type text,
        expiry date,
        strike double precision,
        option_right text,
        position_qty double precision NOT NULL,
        trade_qty double precision NOT NULL,
        avg_cost double precision,
        mark double precision,
        mark_source text,
        underlying_close double precision,
        delta double precision,
        gamma double precision,
        vega double precision,
        theta double precision,
        iv double precision,
        greeks_asof timestamptz,
        positions_updated_at timestamptz,
        captured_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT position_snapshot_daily_uq
            UNIQUE NULLS NOT DISTINCT (snapshot_date, account_id, contract_key, trade_id)
    )
    """,
    f"""
    CREATE INDEX IF NOT EXISTS position_snapshot_daily_trade_ix
        ON {POSITION_SNAPSHOT_DAILY} (trade_id, snapshot_date) WHERE trade_id IS NOT NULL
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {ACCOUNT_NAV_DAILY} (
        account_nav_daily_id bigserial PRIMARY KEY,
        snapshot_date date NOT NULL,
        account_id text NOT NULL,
        net_liquidation double precision,
        total_cash double precision,
        buying_power double precision,
        account_updated_at timestamptz,
        captured_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT account_nav_daily_uq UNIQUE (snapshot_date, account_id)
    )
    """,
    # 0.51.0: margin pressure (nullable, no default: no rewrite, no backfill).
    f"ALTER TABLE {ACCOUNT_NAV_DAILY} ADD COLUMN IF NOT EXISTS cushion double precision",
    f"ALTER TABLE {ACCOUNT_NAV_DAILY} ADD COLUMN IF NOT EXISTS excess_liquidity double precision",
    f"ALTER TABLE {ACCOUNT_NAV_DAILY} ADD COLUMN IF NOT EXISTS maint_margin_req double precision",
)


def ensure_snapshot_tables(cur: Any, log_table: Optional[Callable[[str, str], None]] = None) -> None:
    """Create the two snapshot tables, the index and the 0.51.0 NAV columns when missing; a no-op otherwise."""
    if callable(log_table):
        log_table(POSITION_SNAPSHOT_DAILY, "Daily position snapshot per trade (W4)")
        log_table(ACCOUNT_NAV_DAILY, "Daily account NAV (W4)")
    for stmt in SNAPSHOT_DDL:
        cur.execute(stmt)
