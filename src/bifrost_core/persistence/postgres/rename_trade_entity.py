"""Naming R3: rename the Trade entity in one env database (one transaction; core 0.45.0).

``strategy_instance`` -> ``trade``, ``strategy_instance_execution`` -> ``trade_execution``
(``allocated_quantity`` -> ``split_quantity``), ``strategy_plan`` / ``trade_review``
``strategy_instance_id`` -> ``trade_id``, ``trade_review.tags_*`` -> ``tags_*_json``, with their
sequences, constraints and indexes (REQUEST-naming-program-decision-pack-2026-10-03 §5.2;
Owner-approved D1-A, D2-A, D7-A). Only names change: ``ALTER … RENAME`` rewrites no data.

``forward_sql(env)`` prints the whole transaction for ``bifrost_<env>`` (``psql -d bifrost_<env>``,
as ``postgres``); it ends ``ROLLBACK`` unless ``commit``. Steps:

0. guards: the right database, the brokerage views owned by whom this env expects, ``trade``
   not there yet, ``strategy_instance`` still a table; then the counts the report compares
   (``r3_before``), read before anything changes;
1. drop the five env views that depend on the renamed tables (their columns change);
2-4. the renames (pack §5.2, in that order);
5. the one-version compatibility views ``public.strategy_instance`` and
   ``public.strategy_instance_execution`` (old column names; auto-updatable, so a pod still on
   core < 0.45.0 keeps reading and writing trades and attributions through them);
6. the env views exactly as core builds them (``_create_brokerage_views(env=True)``): the
   execution views with ``trade_id`` / ``ib_trade_id`` / ``ib_related_trade_id`` and the
   one-version ``strategy_instance_id``, ``brokerage.trade_fill_splits`` and the compatibility
   view ``brokerage.instance_allocations``;
7. DEV only: the brokerage views belong to ``postgres`` there (bifrost on STG / PROD), so steps
   1 and 6 run after ``RESET ROLE`` and are followed by ``GRANT SELECT … TO bifrost``;
8. the report: each count before and after, and the transaction stops (RAISE) if one differs.

Golden Source is not touched. ``account_execution_instance_allocation`` (frozen, D7-A) keeps
its column; its FK follows ``trade`` by OID and the table goes in R4. The reverse is
``rename_trade_entity_reverse.reverse_sql``.
"""

from __future__ import annotations

from typing import List, Tuple

APP_ROLE = "bifrost"

# env -> (database, owner of the brokerage views there). Read 2026-10-04 (read-only): DEV's
# views were rebuilt by postgres during TD-09; STG / PROD's belong to the app role.
ENVS = {
    "dev": ("bifrost_dev", "postgres"),
    "stg": ("bifrost_stg", "bifrost"),
    "prod": ("bifrost_prod", "bifrost"),
}

# The env views that read the renamed tables, in a drop order that never trips on a dependency.
OLD_ENV_VIEWS: Tuple[str, ...] = (
    "brokerage.instance_allocations",
    "brokerage.executions_tws",
    "brokerage.executions_fly",
    "brokerage.executions_final",
    "brokerage.executions",
)
NEW_ENV_VIEWS: Tuple[str, ...] = (
    "brokerage.instance_allocations",
    "brokerage.trade_fill_splits",
    "brokerage.executions_tws",
    "brokerage.executions_fly",
    "brokerage.executions_final",
    "brokerage.executions",
)

# (kind, table the statement runs on, old name, new name). kind: table / column / sequence /
# constraint / index. Steps 2-4 of the pack, in order.
RENAMES: Tuple[Tuple[str, str, str, str], ...] = (
    ("table", "", "strategy_instance", "trade"),
    ("column", "trade", "strategy_instance_id", "trade_id"),
    ("sequence", "", "strategy_instance_strategy_instance_id_seq", "trade_trade_id_seq"),
    ("constraint", "trade", "strategy_instance_pkey", "trade_pkey"),
    ("constraint", "trade", "strategy_instance_id_account_uq", "trade_id_account_uq"),
    (
        "constraint",
        "trade",
        "strategy_instance_strategy_opportunity_id_fkey",
        "trade_strategy_opportunity_id_fkey",
    ),
    ("index", "", "strategy_instance_opportunity_id", "trade_opportunity_id"),
    ("index", "", "strategy_instance_account_opened", "trade_account_opened"),
    ("table", "", "strategy_instance_execution", "trade_execution"),
    ("column", "trade_execution", "strategy_instance_execution_id", "trade_execution_id"),
    ("column", "trade_execution", "strategy_instance_id", "trade_id"),
    ("column", "trade_execution", "allocated_quantity", "split_quantity"),
    (
        "sequence",
        "",
        "strategy_instance_execution_strategy_instance_execution_id_seq",
        "trade_execution_trade_execution_id_seq",
    ),
    ("constraint", "trade_execution", "strategy_instance_execution_pkey", "trade_execution_pkey"),
    ("constraint", "trade_execution", "strategy_instance_execution_instance_fk", "trade_execution_trade_fk"),
    ("constraint", "trade_execution", "strategy_instance_execution_uq", "trade_execution_uq"),
    ("constraint", "trade_execution", "strategy_instance_execution_qty_ck", "trade_execution_qty_ck"),
    ("index", "", "strategy_instance_execution_whole_uq", "trade_execution_whole_uq"),
    ("index", "", "strategy_instance_execution_instance_ix", "trade_execution_trade_ix"),
    ("column", "strategy_plan", "strategy_instance_id", "trade_id"),
    ("constraint", "strategy_plan", "strategy_plan_strategy_instance_id_fkey", "strategy_plan_trade_id_fkey"),
    ("index", "", "strategy_plan_instance", "strategy_plan_trade"),
    ("column", "trade_review", "strategy_instance_id", "trade_id"),
    ("constraint", "trade_review", "trade_review_strategy_instance_id_fkey", "trade_review_trade_id_fkey"),
    ("constraint", "trade_review", "trade_review_strategy_instance_id_key", "trade_review_trade_id_key"),
    ("column", "trade_review", "tags_added", "tags_added_json"),
    ("column", "trade_review", "tags_dropped", "tags_dropped_json"),
)

# Step 5: one version, dropped in R4. No ``notes``: the column was dropped on 2026-10-03.
COMPAT_VIEWS: Tuple[str, ...] = (
    """CREATE VIEW public.strategy_instance AS
  SELECT trade_id AS strategy_instance_id, strategy_opportunity_id, account_id, opened_at,
         label, created_at, updated_at
  FROM public.trade""",
    """CREATE VIEW public.strategy_instance_execution AS
  SELECT trade_execution_id AS strategy_instance_execution_id, account_id, exec_id,
         trade_id AS strategy_instance_id, split_quantity AS allocated_quantity, created_at, updated_at
  FROM public.trade_execution""",
)


def rename_statement(kind: str, table: str, old: str, new: str) -> str:
    """One ALTER … RENAME (public schema)."""
    if kind == "table":
        return f"ALTER TABLE public.{old} RENAME TO {new}"
    if kind == "column":
        return f"ALTER TABLE public.{table} RENAME COLUMN {old} TO {new}"
    if kind == "sequence":
        return f"ALTER SEQUENCE public.{old} RENAME TO {new}"
    if kind == "constraint":
        return f"ALTER TABLE public.{table} RENAME CONSTRAINT {old} TO {new}"
    if kind == "index":
        return f"ALTER INDEX public.{old} RENAME TO {new}"
    raise ValueError(f"unknown rename kind {kind!r}")


def env_target(env: str) -> Tuple[str, str]:
    """(database, owner of its brokerage views) for ``env``; ValueError for anything else."""
    if env not in ENVS:
        raise ValueError(f"env must be one of {', '.join(ENVS)}, not {env!r}")
    return ENVS[env]


def env_guards(env: str) -> List[str]:
    """The right database, and its brokerage views owned by whom the script expects."""
    db, owner = env_target(env)
    return [
        f"""DO $r3$ BEGIN
  IF current_database() <> '{db}' THEN
    RAISE EXCEPTION 'R3: connected to %, but this SQL is for {db}', current_database();
  END IF;
  IF (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = to_regclass('brokerage.executions'))
     IS DISTINCT FROM '{owner}' THEN
    RAISE EXCEPTION 'R3: brokerage.executions is owned by %, this SQL expects {owner} (regenerate with the right --env)',
      (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = to_regclass('brokerage.executions'));
  END IF;
END $r3$""",
    ]


def as_view_owner(env: str, statements: List[str], views: Tuple[str, ...], *, grant: bool) -> List[str]:
    """``statements`` run as the brokerage views' owner: on DEV (postgres) after ``RESET ROLE``,
    then ``GRANT SELECT`` on ``views`` to the app role (``grant``) and back to it."""
    _, owner = env_target(env)
    if owner == APP_ROLE:
        return list(statements)
    out = ["RESET ROLE", *statements]
    if grant:
        out.append(f"GRANT SELECT ON {', '.join(views)} TO {APP_ROLE}")
    out.append(f"SET LOCAL ROLE {APP_ROLE}")
    return out


class _Recorder:
    """A cursor stand-in that keeps the SQL ``_create_brokerage_views`` would run."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        if params is not None:
            raise ValueError("view DDL takes no parameters")
        self.statements.append(sql)


def view_statements(schema: str = "brokerage") -> List[str]:
    """Step 6: the env views as ``setup_fdw_foreign_tables`` builds them (this core's code)."""
    from bifrost_core.persistence.postgres.brokerage_views import _create_brokerage_views

    rec = _Recorder()
    _create_brokerage_views(rec, schema, env=True)
    return rec.statements


# (label, count before -- old names, count after -- new names). The compatibility rows read
# the old names after the rename and must see what the tables held before it.
REPORT: Tuple[Tuple[str, str, str], ...] = (
    ("trade", "SELECT count(*) FROM public.strategy_instance", "SELECT count(*) FROM public.trade"),
    (
        "trade_execution",
        "SELECT count(*) FROM public.strategy_instance_execution",
        "SELECT count(*) FROM public.trade_execution",
    ),
    (
        "splits",
        "SELECT count(*) FROM public.strategy_instance_execution WHERE allocated_quantity IS NOT NULL",
        "SELECT count(*) FROM public.trade_execution WHERE split_quantity IS NOT NULL",
    ),
    (
        "view_attributed",
        "SELECT count(*) FROM brokerage.executions WHERE strategy_instance_id IS NOT NULL",
        "SELECT count(*) FROM brokerage.executions WHERE trade_id IS NOT NULL",
    ),
    (
        "view_splits",
        "SELECT count(*) FROM brokerage.instance_allocations",
        "SELECT count(*) FROM brokerage.trade_fill_splits",
    ),
    (
        "compat_strategy_instance",
        "SELECT count(*) FROM public.strategy_instance",
        "SELECT count(*) FROM public.strategy_instance",
    ),
    (
        "compat_instance_allocations",
        "SELECT count(*) FROM brokerage.instance_allocations",
        "SELECT count(*) FROM brokerage.instance_allocations",
    ),
)


def before_statement(report: Tuple[Tuple[str, str, str], ...]) -> str:
    """The counts read before anything changes, kept in a temp table for the report."""
    cols = ",\n  ".join(f"({before}) AS {label}" for label, before, _ in report)
    return f"CREATE TEMP TABLE r3_before ON COMMIT DROP AS SELECT\n  {cols}"


def report_statements(report: Tuple[Tuple[str, str, str], ...]) -> List[str]:
    """Each count before and after (printed), then RAISE when one differs."""
    rows = "\nUNION ALL ".join(
        f"SELECT '{label}' AS what, b.{label} AS before, ({after}) AS after FROM r3_before b"
        for label, _, after in report
    )
    checks = "\n  OR ".join(f"b.{label} <> ({after})" for label, _, after in report)
    return [
        f"SELECT what, before, after, before = after AS same FROM (\n{rows}\n) r",
        f"""DO $r3$ BEGIN
  IF EXISTS (SELECT 1 FROM r3_before b WHERE {checks}) THEN
    RAISE EXCEPTION 'R3: a count changed across the rename (see the report above); nothing is kept';
  END IF;
END $r3$""",
    ]


def forward_statements(env: str) -> List[str]:
    """Every statement of the forward transaction, without BEGIN / COMMIT."""
    parts: List[str] = ["SET LOCAL lock_timeout = '5s'", f"SET LOCAL ROLE {APP_ROLE}"]
    parts += env_guards(env)
    parts.append(
        """DO $r3$ BEGIN
  IF to_regclass('public.trade') IS NOT NULL THEN
    RAISE EXCEPTION 'R3: public.trade already exists (already migrated?)';
  END IF;
  IF (SELECT relkind FROM pg_class WHERE oid = to_regclass('public.strategy_instance')) IS DISTINCT FROM 'r' THEN
    RAISE EXCEPTION 'R3: public.strategy_instance is not a base table (already migrated?)';
  END IF;
END $r3$"""
    )
    parts += ["DROP TABLE IF EXISTS pg_temp.r3_before", before_statement(REPORT)]
    parts += as_view_owner(env, [f"DROP VIEW {v}" for v in OLD_ENV_VIEWS], (), grant=False)
    parts += [rename_statement(*r) for r in RENAMES]
    parts += list(COMPAT_VIEWS)
    views = [s.strip() for s in view_statements()]
    parts += as_view_owner(env, views, NEW_ENV_VIEWS, grant=True)
    parts += report_statements(REPORT)
    return parts


def render(parts: List[str], *, commit: bool, title: str) -> str:
    """One transaction: a header comment, BEGIN, the statements, COMMIT or ROLLBACK."""
    body = ";\n\n".join(p.rstrip().rstrip(";") for p in parts)
    end = "COMMIT" if commit else "ROLLBACK"
    head = f"-- {title} ({'commit' if commit else 'dry run: ends ROLLBACK'})\n"
    return f"{head}BEGIN;\n\n{body};\n\n{end};\n"


def forward_sql(env: str, *, commit: bool = False) -> str:
    """The R3 rename for ``bifrost_<env>``, one transaction; ROLLBACK unless ``commit``."""
    db, _ = env_target(env)
    return render(forward_statements(env), commit=commit, title=f"naming R3 rename, {db}, core 0.45.0")


__all__ = [
    "APP_ROLE",
    "COMPAT_VIEWS",
    "ENVS",
    "NEW_ENV_VIEWS",
    "OLD_ENV_VIEWS",
    "RENAMES",
    "REPORT",
    "forward_sql",
    "forward_statements",
    "rename_statement",
    "render",
    "view_statements",
]
