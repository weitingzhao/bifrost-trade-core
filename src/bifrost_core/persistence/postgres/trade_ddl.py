"""The Trade entity's tables: trade, its fill attribution, plans and reviews (naming R3, core 0.45.0).

Split out of ``ddl`` (which calls ``ensure_trade_tables``) when the entity was renamed:
``strategy_instance`` -> ``trade`` (PK ``trade_id``), ``strategy_instance_execution`` ->
``trade_execution`` (``allocated_quantity`` -> ``split_quantity``), the ``strategy_instance_id``
FK columns of ``strategy_plan`` / ``trade_review`` -> ``trade_id``, and ``trade_review.tags_*``
-> ``tags_*_json`` (REQUEST-naming-program-decision-pack-2026-10-03, D1-A / D2-A).

A new database gets the new names here. An existing one is renamed by the Owner's one-off
step (core 0.45.0's ``scripts/db/rename_trade_entity.py``, one transaction per env, retired in
0.47.0 after it ran on all three envs; the SQL is kept in the infra db-steps), never by db-init:
``refuse_unmigrated_trade_entity`` stops ``_ensure_tables`` before it changes anything while
``public.strategy_instance`` is still a table, because creating ``trade`` beside it would
split the book in two. After the rename every statement here is a no-op.

Naming R4 (core 0.47.0): R3's compatibility views ``public.strategy_instance`` /
``public.strategy_instance_execution`` and the frozen pre-TD-09 split table
``account_execution_instance_allocation`` are no longer created or named here; the Owner's R4
db-step drops them (the table after a CSV export, D7-A).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

TRADE = "trade"

# The un-migrated shape: the old entity table is still a base table.
UNMIGRATED_MESSAGE = (
    "public.strategy_instance is still a table: this database is not on the naming R3 rename "
    "(core 0.45.0 names it public.trade). Run the Owner's R3 step first -- core 0.45.0's "
    "scripts/db/rename_trade_entity.py, committed per env in bifrost-trade-infra "
    "scripts/release/db-steps.d/sql/2026-10-04-r3-rename-trade-entity-<env>.sql -- "
    "and run db-init again. Nothing was changed."
)


def refuse_unmigrated_trade_entity(cur: Any) -> None:
    """Raise RuntimeError when ``public.strategy_instance`` is a base table (not yet renamed).

    A view of that name is R3's compatibility view (until the R4 db-step drops it) and is
    fine; no object at all is a renamed or a new database. Mirrors the TD-09 precheck in ``setup_fdw_foreign_tables``: stop before the
    first change, with the command that fixes it."""
    cur.execute(
        "SELECT c.relkind FROM pg_class c WHERE c.oid = to_regclass('public.strategy_instance')"
    )
    row = cur.fetchone()
    if row is not None and row[0] in ("r", "p"):
        raise RuntimeError(UNMIGRATED_MESSAGE)


# TD-09 (core 0.37.0), renamed in R3: trade attribution per env, keyed by the fill
# (account_id, exec_id) -- a TWS row and its Flex twin share it. A NULL split_quantity is the
# whole fill; split rows carry their share. The composite FK makes "the fill's account is the
# trade's account" a rule of the database rather than a Python check. Idempotent.
TRADE_EXECUTION_DDL: tuple[str, ...] = (
    """
    DO $te$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'trade_id_account_uq'
          AND conrelid = 'public.trade'::regclass
      ) THEN
        ALTER TABLE trade
          ADD CONSTRAINT trade_id_account_uq UNIQUE (trade_id, account_id);
      END IF;
    END $te$;
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_execution (
        trade_execution_id bigserial PRIMARY KEY,
        account_id text NOT NULL,
        exec_id text NOT NULL,
        trade_id bigint NOT NULL,
        split_quantity numeric NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT trade_execution_trade_fk
            FOREIGN KEY (trade_id, account_id)
            REFERENCES trade (trade_id, account_id) ON DELETE RESTRICT,
        CONSTRAINT trade_execution_uq UNIQUE (account_id, exec_id, trade_id),
        CONSTRAINT trade_execution_qty_ck
            CHECK (split_quantity IS NULL OR split_quantity <> 0)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS trade_execution_whole_uq "
    "ON trade_execution (account_id, exec_id) WHERE split_quantity IS NULL",
    "CREATE INDEX IF NOT EXISTS trade_execution_trade_ix "
    "ON trade_execution (trade_id)",
)

_TRADE_SQL = """
CREATE TABLE IF NOT EXISTS trade (
    trade_id bigserial PRIMARY KEY,
    strategy_opportunity_id bigint NOT NULL REFERENCES strategy_opportunity(strategy_opportunity_id) ON DELETE RESTRICT,
    account_id text NOT NULL,
    opened_at timestamptz NOT NULL,
    label text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

_STRATEGY_PLAN_SQL = """
CREATE TABLE IF NOT EXISTS strategy_plan (
    strategy_plan_id        bigserial PRIMARY KEY,
    account_id              text        NOT NULL,
    symbol                  text        NOT NULL,
    structure_label         text        NOT NULL,
    strategy_structure_id   bigint      REFERENCES strategy_structure(strategy_structure_id) ON DELETE SET NULL,
    strategy_opportunity_id bigint      REFERENCES strategy_opportunity(strategy_opportunity_id) ON DELETE SET NULL,
    legs_json               jsonb       NOT NULL DEFAULT '[]'::jsonb,
    qty                     integer     NOT NULL CHECK (qty > 0),
    price_effect            text        CHECK (price_effect IN ('credit', 'debit')),
    limit_price             numeric     CHECK (limit_price >= 0),
    target_kind             text        CHECK (target_kind IN ('credit_pct', 'option_price', 'underlying_price')),
    target_value            numeric,
    stop_kind               text        CHECK (stop_kind IN ('credit_multiple', 'option_price', 'underlying_price')),
    stop_value              numeric,
    exit_by                 date,
    rationale               text,
    source_kind             text        NOT NULL DEFAULT 'manual'
                                        CHECK (source_kind IN ('manual', 'symbol', 'hypothesis', 'inbox_draft', 'roll')),
    source_ref              text,
    source_json             jsonb       NOT NULL DEFAULT '[]'::jsonb,
    status                  text        NOT NULL DEFAULT 'draft'
                                        CHECK (status IN ('draft', 'intended', 'filled', 'cancelled')),
    expires_at              timestamptz,
    intended_at             timestamptz,
    cancelled_at            timestamptz,
    trade_id                bigint      REFERENCES trade(trade_id) ON DELETE RESTRICT,
    parent_strategy_plan_id bigint      REFERENCES strategy_plan(strategy_plan_id) ON DELETE SET NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CHECK ((target_kind IS NULL) = (target_value IS NULL)),
    CHECK ((stop_kind IS NULL) = (stop_value IS NULL)),
    CONSTRAINT strategy_plan_filled_instance_ck
        CHECK ((status = 'filled') = (trade_id IS NOT NULL))
)
"""

_TRADE_REVIEW_SQL = """
CREATE TABLE IF NOT EXISTS trade_review (
    trade_review_id      bigserial   PRIMARY KEY,
    trade_id             bigint      NOT NULL UNIQUE
                                     REFERENCES trade(trade_id) ON DELETE RESTRICT,
    tags_added_json      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    tags_dropped_json    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    reviewed_at          timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
)
"""


def ensure_trade_tables(cur: Any, log_table: Optional[Callable[[str, str], None]] = None) -> None:
    """Create the Trade entity's tables and indexes if missing. Run after ``strategy_opportunity``
    and ``strategy_structure`` exist, and after ``refuse_unmigrated_trade_entity``."""

    def _log_table(name: str, purpose: str) -> None:
        if callable(log_table):
            log_table(name, purpose)

    # Not created, never added back (core 0.43.0, TD-43 / TD-73): strategy_instance.notes,
    # strategy_plan.filled_at, trade_review.note -- dropped by an Owner db-step, not here.
    _log_table(TRADE, "Trade: a position opened under an opportunity, one account (was strategy_instance)")
    cur.execute(_TRADE_SQL)
    cur.execute("CREATE INDEX IF NOT EXISTS trade_opportunity_id ON trade (strategy_opportunity_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS trade_account_opened ON trade (account_id, opened_at)")

    _log_table("strategy_plan", "Structured trade plans (advisory; no execution consumer -- D10)")
    cur.execute(_STRATEGY_PLAN_SQL)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS strategy_plan_status_created ON strategy_plan (status, created_at DESC)"
    )
    cur.execute("CREATE INDEX IF NOT EXISTS strategy_plan_symbol ON strategy_plan (symbol)")
    cur.execute(
        "CREATE INDEX IF NOT EXISTS strategy_plan_trade ON strategy_plan (trade_id) WHERE trade_id IS NOT NULL"
    )

    _log_table("trade_review", "One review record per trade (Review › Queue and Single trade)")
    cur.execute(_TRADE_REVIEW_SQL)

    _log_table(
        "trade_execution",
        "A fill (account_id, exec_id) attributed to this env's trade; splits carry split_quantity (TD-09, R3)",
    )
    for stmt in TRADE_EXECUTION_DDL:
        cur.execute(stmt)


__all__ = [
    "TRADE",
    "TRADE_EXECUTION_DDL",
    "UNMIGRATED_MESSAGE",
    "ensure_trade_tables",
    "refuse_unmigrated_trade_entity",
]
