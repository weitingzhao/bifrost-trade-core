"""What the Ops platform may know about a Trade database without knowing its tables (D8-A).

The platform's freshness panel and data clone used to name Trade tables themselves
(``strategy_instance``, ``strategy_opportunity``, ``watchlist`` …). Trade renames its
tables (naming program R3), and the platform must not learn Trade concepts (D13), so
Trade answers the questions the platform asks, by role rather than by table:

- ``activity``: when this database last changed, one row per source;
- ``sample``: a row count that says the database holds the book (clone verification);
- ``clone_groups``: the tables a selective clone must take together. Each group is its
  seed tables plus every table that references them, transitively -- exactly what
  ``TRUNCATE … CASCADE`` on the seeds would empty -- read from ``pg_constraint`` at
  request time, so a new child table is never left out;
- ``watchlist``: the optionable stocks this env watches (``sec_type = 'STK'`` and
  ``optionable``), trimmed, upper-cased, distinct and sorted. The platform unions them
  across envs for the market-data plugin (``GET /api/v1/watchlist/union``) instead of
  selecting from ``public.watchlist`` itself (core 0.44.0).

The seeds below are Trade's own names. Each role names its table as a list of candidates,
newest first, and the first one that is a *table* (``relkind`` r / p) answers (a view has no
rows of its own to clone and ``TRUNCATE`` refuses it). Naming R3 (core 0.45.0) renamed
``strategy_instance`` to ``trade``; from 0.45.0 to 0.46.x the old name was the trade role's
second candidate, and naming R4 (core 0.47.0) dropped it -- db-init refuses a database that
is not renamed (``trade_ddl.refuse_unmigrated_trade_entity``). A role with no table at all is
reported with ``last_ts: null`` / a ``detail``, never dropped. Read-only: every statement
is a SELECT.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# The trade table (named so since naming R3, core 0.45.0).
TRADE_TABLES: Tuple[str, ...] = ("trade",)

# (source label, candidate tables, timestamp column)
ACTIVITY_SOURCES: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("trades", TRADE_TABLES, "updated_at"),
    ("opportunities", ("strategy_opportunity",), "updated_at"),
    ("watchlist", ("watchlist",), "created_at"),
)

# (label, candidate tables) counted for clone verification.
SAMPLE: Tuple[str, Tuple[str, ...]] = ("trades", TRADE_TABLES)

# (label, table) whose optionable stocks the platform unions across envs.
WATCHLIST: Tuple[str, str] = ("optionable_stocks", "watchlist")

_WATCHLIST_SQL = """
SELECT DISTINCT upper(trim(symbol)) AS symbol
FROM {table}
WHERE sec_type = 'STK' AND optionable = true AND symbol IS NOT NULL AND trim(symbol) <> ''
ORDER BY 1
"""

# (group name, seeds -- each a tuple of candidate tables --, note)
CLONE_GROUPS: Tuple[Tuple[str, Tuple[Tuple[str, ...], ...], str], ...] = (
    ("trades", (TRADE_TABLES,), "Trades with their fill attributions, splits, reviews and plans."),
    ("rules", (("strategy_opportunity",),), "Opportunities and everything built on them, trades included."),
    (
        "position_categories",
        (("preference_position_categories",),),
        "Position categories and their tags.",
    ),
    ("watchlist", (("watchlist",),), "The watchlist."),
)

_CLOSURE_SQL = """
WITH RECURSIVE closure(oid) AS (
    SELECT c.oid FROM pg_class c WHERE c.oid = ANY(%s::regclass[])
    UNION
    SELECT con.conrelid
    FROM pg_constraint con
    JOIN closure ON con.confrelid = closure.oid
    WHERE con.contype = 'f'
)
SELECT CASE WHEN n.nspname = 'public' THEN cl.relname ELSE n.nspname || '.' || cl.relname END AS name
FROM closure
JOIN pg_class cl ON cl.oid = closure.oid
JOIN pg_namespace n ON n.oid = cl.relnamespace
ORDER BY 1
"""


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


_TABLE_SQL = "SELECT EXISTS (SELECT 1 FROM pg_class WHERE oid = to_regclass(%s) AND relkind IN ('r', 'p'))"


def _table(cur: Any, candidates: Sequence[str]) -> Optional[str]:
    """The first of ``candidates`` that is a table (not a view), or None."""
    for t in candidates:
        cur.execute(_TABLE_SQL, (t,))
        row = cur.fetchone()
        if row and row[0]:
            return t
    return None


def _activity(cur: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for source, candidates, column in ACTIVITY_SOURCES:
        item: Dict[str, Any] = {"source": source, "last_ts": None}
        table = _table(cur, candidates)
        if table is None:
            item["detail"] = "missing"
            out.append(item)
            continue
        # Identifiers come from the constants above, never from the request.
        cur.execute(f"SELECT max({column}) FROM {table}")
        row = cur.fetchone()
        item["last_ts"] = _iso(row[0]) if row else None
        out.append(item)
    return out


def _sample(cur: Any) -> Dict[str, Any]:
    label, candidates = SAMPLE
    table = _table(cur, candidates)
    if table is None:
        return {"label": label, "rows": None, "detail": "missing"}
    cur.execute(f"SELECT count(*) FROM {table}")
    row = cur.fetchone()
    return {"label": label, "rows": int(row[0]) if row and row[0] is not None else 0}


def _watchlist(cur: Any) -> Dict[str, Any]:
    """``{label, symbols, count}``; a missing table is ``symbols: null`` with a ``detail``,
    never an empty list -- an empty list means the env watches no optionable stock."""
    label, table = WATCHLIST
    if _table(cur, (table,)) is None:
        return {"label": label, "symbols": None, "count": None, "detail": "missing"}
    cur.execute(_WATCHLIST_SQL.format(table=table))
    symbols = [str(r[0]) for r in cur.fetchall() or [] if r and r[0]]
    return {"label": label, "symbols": symbols, "count": len(symbols)}


def _clone_groups(cur: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for name, seeds, note in CLONE_GROUPS:
        found = [(c, _table(cur, c)) for c in seeds]
        present = [t for _, t in found if t is not None]
        tables: List[str] = []
        if present:
            cur.execute(_CLOSURE_SQL, (list(present),))
            closure = [str(r[0]) for r in cur.fetchall() or []]
            # Seeds first, then the tables that reference them, alphabetically.
            tables = [*present, *[t for t in closure if t not in present]]
        group: Dict[str, Any] = {"name": name, "tables": tables, "note": note}
        missing = [c[0] for c, t in found if t is None]
        if missing:
            group["detail"] = "missing: " + ", ".join(missing)
        out.append(group)
    return out


def read_data_probe(conn: Any) -> Dict[str, Any]:
    """``{generated_at, activity, sample, clone_groups, watchlist}`` for the database ``conn`` is on.

    Raises whatever the database raises; the caller decides how a failed read answers."""
    with conn.cursor() as cur:
        activity = _activity(cur)
        sample = _sample(cur)
        groups = _clone_groups(cur)
        watchlist = _watchlist(cur)
    return {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "activity": activity,
        "sample": sample,
        "clone_groups": groups,
        "watchlist": watchlist,
    }
