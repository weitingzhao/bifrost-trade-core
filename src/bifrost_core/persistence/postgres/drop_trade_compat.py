"""Naming R4: drop what R3 kept for one version, in one env database (one transaction; core 0.47.0).

REQUEST-naming-program-decision-pack-2026-10-03 §5.5 (Owner-approved, D4-A / D7-A). R3 (core
0.45.0, 2026-10-04) renamed the Trade entity and left, for pods still on core < 0.45.0:

- the compatibility views ``public.strategy_instance`` and ``public.strategy_instance_execution``;
- the env view ``brokerage.instance_allocations`` and the env views' ``strategy_instance_id``
  column (= ``trade_id``);
- the frozen pre-TD-09 split table ``public.account_execution_instance_allocation`` (2 rows per
  env, each also in ``trade_execution``; created by db-init up to core 0.46.x).

No pod has needed any of them since core 0.45.0 (every statement names the new objects).

**db-init does not rebuild the env views in dev / stg / prod**: its FDW step
(``setup_fdw_foreign_tables``) stops at ``must be owner of foreign server golden_source_server``
and the Job logs ``FDW setup skipped`` (Loki, every db-init of 2026-10-01..03). R3's step rebuilt
them itself; so does this one -- step 2 is exactly what core 0.47.0's
``_create_brokerage_views(env=True)`` runs (it drops ``instance_allocations`` by name and
recreates the five views without ``strategy_instance_id``).

The step runs **after** the env's core 0.47.0 deploy: up to 0.46.x db-init creates the frozen
table again (``CREATE TABLE IF NOT EXISTS``, empty) on every release.

``forward_sql(env)`` prints the whole transaction for ``bifrost_<env>`` (``psql -d bifrost_<env>``,
as ``postgres``; it switches to ``bifrost``, which owns every object here in all three envs since
TD-85 D8); it ends ``ROLLBACK`` unless ``commit``. The rebuilt views lose their grants with the
DROP; bifrost's default privileges (TD-85 D1) give ``trade_app_<env>`` SELECT again, and step 2
also grants it explicitly, so the runtime role reads them whatever the default ACLs say. Steps:

0. guards: the right database; the two compatibility objects are views (or already gone), never
   tables; every object dropped or rebuilt is owned by ``bifrost``; the legacy table holds exactly
   ``LEGACY_ROWS`` rows and each is in ``trade_execution`` (same fill, trade and quantity) -- the
   CSV export (``EXPORT_SQL``) is taken before the commit; then the counts the report compares
   (``r4_before``);
1. ``DROP VIEW`` the two public compatibility views;
2. the env views as core 0.47.0 builds them (``view_statements``), then ``GRANT SELECT`` on the five
   to the env's runtime role ``trade_app_<env>`` (TD-85) when that role exists;
3. ``DROP TABLE public.account_execution_instance_allocation`` (its sequence, indexes and FK go
   with it);
4. the report: counts before / after (trade, trade_execution, splits, attributed fills, split
   rows) and a RAISE if one changed, an object is left, a view still has ``strategy_instance_id``
   or ``trade_app_<env>`` (when it exists) cannot read a rebuilt view.

``reverse_sql(env)`` puts back the two views (R3's definitions), ``brokerage.instance_allocations``
(core 0.45.0's) and the empty table (core 0.46.x's DDL); the rows come back from the CSV with
``RESTORE_SQL`` / ``SEQUENCE_SQL``. It leaves the five env views as they are: no pod on core
0.45.0 or later reads ``strategy_instance_id`` from them. Golden Source is not touched.
"""

from __future__ import annotations

from typing import List, Tuple

APP_ROLE = "bifrost"

ENVS = {
    "dev": "bifrost_dev",
    "stg": "bifrost_stg",
    "prod": "bifrost_prod",
}

LEGACY_TABLE = "public.account_execution_instance_allocation"
# Frozen since core 0.37.0 (no writer): 2 rows in each of dev / stg / prod, read 2026-10-04.
LEGACY_ROWS = 2

COMPAT_VIEWS: Tuple[str, ...] = ("public.strategy_instance_execution", "public.strategy_instance")
COMPAT_ENV_VIEW = "brokerage.instance_allocations"
DROPPED: Tuple[str, ...] = (*COMPAT_VIEWS, COMPAT_ENV_VIEW, LEGACY_TABLE)
# Rebuilt by step 2 (dropped and created again, so they must be bifrost's too).
ENV_VIEWS: Tuple[str, ...] = (
    "brokerage.executions",
    "brokerage.executions_final",
    "brokerage.executions_fly",
    "brokerage.executions_tws",
    "brokerage.trade_fill_splits",
)

# Reverse: the objects as R3 / core 0.45.0 / core 0.46.x made them.
R3_COMPAT_VIEWS: Tuple[str, ...] = (
    """CREATE VIEW public.strategy_instance AS
  SELECT trade_id AS strategy_instance_id, strategy_opportunity_id, account_id, opened_at,
         label, created_at, updated_at
  FROM public.trade""",
    """CREATE VIEW public.strategy_instance_execution AS
  SELECT trade_execution_id AS strategy_instance_execution_id, account_id, exec_id,
         trade_id AS strategy_instance_id, split_quantity AS allocated_quantity, created_at, updated_at
  FROM public.trade_execution""",
)
R3_ENV_VIEW = """CREATE VIEW brokerage.instance_allocations AS
  SELECT account_id, account_executions_id, trade_id AS strategy_instance_id,
         quantity AS allocated_quantity, exec_id
  FROM brokerage.trade_fill_splits"""
LEGACY_TABLE_DDL: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS public.account_execution_instance_allocation (
    account_execution_instance_allocation_id bigserial PRIMARY KEY,
    account_id text NOT NULL,
    account_executions_id bigint NOT NULL,
    strategy_instance_id bigint NOT NULL REFERENCES public.trade(trade_id) ON DELETE RESTRICT,
    allocated_quantity double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_executions_id, strategy_instance_id)
)""",
    "CREATE INDEX IF NOT EXISTS account_exec_inst_alloc_account_exec_id "
    "ON public.account_execution_instance_allocation (account_id, account_executions_id)",
    "CREATE INDEX IF NOT EXISTS account_exec_inst_alloc_strategy_instance_id "
    "ON public.account_execution_instance_allocation (strategy_instance_id)",
)

# (label, count) compared before / after: the drop must not move a row of the live objects.
REPORT: Tuple[Tuple[str, str], ...] = (
    ("trade", "SELECT count(*) FROM public.trade"),
    ("trade_execution", "SELECT count(*) FROM public.trade_execution"),
    ("splits", "SELECT count(*) FROM public.trade_execution WHERE split_quantity IS NOT NULL"),
    ("view_attributed", "SELECT count(*) FROM brokerage.executions WHERE trade_id IS NOT NULL"),
    ("view_splits", "SELECT count(*) FROM brokerage.trade_fill_splits"),
)

# The legacy rows that are in trade_execution: the same fill (through the env view's
# account_executions_id), the same trade and the same quantity (pack D7-A, read 2026-10-03/04).
_LEGACY_MATCHED = f"""SELECT count(*) FROM {LEGACY_TABLE} a
  WHERE EXISTS (
    SELECT 1 FROM public.trade_execution te
    JOIN brokerage.executions x ON x.account_id = te.account_id AND x.exec_id = te.exec_id
    WHERE x.account_executions_id = a.account_executions_id
      AND te.account_id = a.account_id
      AND te.trade_id = a.strategy_instance_id
      AND te.split_quantity = a.allocated_quantity::numeric
  )"""


def runtime_role(env: str) -> str:
    """The env's runtime login (TD-85 D1): ``trade_app_<env>``; it reads the env views."""
    env_db(env)
    return f"trade_app_{env}"


def grant_statement(env: str) -> str:
    """Step 2's grant: SELECT on the rebuilt env views to the runtime role, when it exists."""
    role = runtime_role(env)
    views = ", ".join(ENV_VIEWS)
    return f"""DO $r4$ BEGIN
  IF to_regrole('{role}') IS NOT NULL THEN
    GRANT SELECT ON {views} TO {role};
  END IF;
END $r4$"""


def env_db(env: str) -> str:
    """``bifrost_<env>`` for dev / stg / prod; ValueError for anything else."""
    if env not in ENVS:
        raise ValueError(f"env must be one of {', '.join(ENVS)}, not {env!r}")
    return ENVS[env]


def guards(env: str) -> List[str]:
    """Step 0 (see the module docstring), one DO block each."""
    db = env_db(env)
    owners = ", ".join(f"'{o}'" for o in (*DROPPED, *ENV_VIEWS))
    return [
        f"""DO $r4$ BEGIN
  IF current_database() <> '{db}' THEN
    RAISE EXCEPTION 'R4: connected to %, but this SQL is for {db}', current_database();
  END IF;
END $r4$""",
        """DO $r4$ BEGIN
  IF to_regclass('public.trade_execution') IS NULL OR to_regclass('brokerage.executions_raw_flex') IS NULL THEN
    RAISE EXCEPTION 'R4: public.trade_execution or the brokerage FDW tables are missing; nothing is dropped';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_class WHERE oid IN (to_regclass('public.strategy_instance'),
             to_regclass('public.strategy_instance_execution')) AND relkind <> 'v') THEN
    RAISE EXCEPTION 'R4: public.strategy_instance / strategy_instance_execution is not a view (R3 not applied?); nothing is dropped';
  END IF;
END $r4$""",
        f"""DO $r4$ BEGIN
  IF EXISTS (SELECT 1 FROM unnest(ARRAY[{owners}]) o(name)
             JOIN pg_class c ON c.oid = to_regclass(o.name)
             WHERE pg_get_userbyid(c.relowner) <> '{APP_ROLE}') THEN
    RAISE EXCEPTION 'R4: an object to drop or rebuild is not owned by {APP_ROLE}: %',
      (SELECT string_agg(o.name || '=' || pg_get_userbyid(c.relowner), ', ')
         FROM unnest(ARRAY[{owners}]) o(name) JOIN pg_class c ON c.oid = to_regclass(o.name));
  END IF;
END $r4$""",
        f"""DO $r4$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class c ON c.oid = r.ev_class
             WHERE d.refobjid IN (SELECT to_regclass(o) FROM unnest(ARRAY[{owners}]) o)
               AND c.oid <> d.refobjid
               AND c.oid NOT IN (SELECT to_regclass(o) FROM unnest(ARRAY[{owners}]) o WHERE to_regclass(o) IS NOT NULL)) THEN
    RAISE EXCEPTION 'R4: another view depends on an object this step drops or rebuilds (the rebuild uses CASCADE): %',
      (SELECT string_agg(DISTINCT c.oid::regclass::text, ', ') FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid
         JOIN pg_class c ON c.oid = r.ev_class
        WHERE d.refobjid IN (SELECT to_regclass(o) FROM unnest(ARRAY[{owners}]) o) AND c.oid <> d.refobjid
          AND c.oid NOT IN (SELECT to_regclass(o) FROM unnest(ARRAY[{owners}]) o WHERE to_regclass(o) IS NOT NULL));
  END IF;
END $r4$""",
        f"""DO $r4$ BEGIN
  IF to_regclass('{LEGACY_TABLE}') IS NOT NULL THEN
    IF (SELECT count(*) FROM {LEGACY_TABLE}) <> {LEGACY_ROWS} THEN
      RAISE EXCEPTION 'R4: {LEGACY_TABLE} has % rows, expected {LEGACY_ROWS} (the frozen rows): export it again and look before dropping',
        (SELECT count(*) FROM {LEGACY_TABLE});
    END IF;
    IF ({_LEGACY_MATCHED}) <> {LEGACY_ROWS} THEN
      RAISE EXCEPTION 'R4: a row of {LEGACY_TABLE} is not in trade_execution; nothing is dropped';
    END IF;
  END IF;
END $r4$""",
    ]


def before_statement() -> str:
    cols = ",\n  ".join(f"({sql}) AS {label}" for label, sql in REPORT)
    return f"CREATE TEMP TABLE r4_before ON COMMIT DROP AS SELECT\n  {cols}"


def report_statements(env: str) -> List[str]:
    rows = "\nUNION ALL ".join(
        f"SELECT '{label}' AS what, b.{label} AS before, ({sql}) AS after FROM r4_before b" for label, sql in REPORT
    )
    views = ", ".join(f"'{v.split('.', 1)[1]}'" for v in ENV_VIEWS)
    left = " OR ".join(f"to_regclass('{o}') IS NOT NULL" for o in DROPPED) + (
        " OR EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'brokerage'"
        f" AND table_name IN ({views}) AND column_name = 'strategy_instance_id')"
    )
    checks = "\n  OR ".join(f"b.{label} <> ({sql})" for label, sql in REPORT)
    role = runtime_role(env)
    readable = " AND ".join(f"has_table_privilege('{role}', '{v}', 'SELECT')" for v in ENV_VIEWS)
    return [
        f"SELECT what, before, after, before = after AS same FROM (\n{rows}\n) r",
        "SELECT o AS dropped, to_regclass(o) IS NULL AS gone FROM unnest(ARRAY["
        + ", ".join(f"'{o}'" for o in DROPPED)
        + "]) o",
        "SELECT v AS env_view, CASE WHEN to_regrole('" + role + "') IS NULL THEN NULL"
        " ELSE has_table_privilege('" + role + "', v, 'SELECT') END AS " + role + "_select FROM unnest(ARRAY["
        + ", ".join(f"'{v}'" for v in ENV_VIEWS)
        + "]) v",
        f"""DO $r4$ BEGIN
  IF EXISTS (SELECT 1 FROM r4_before b WHERE {checks}) THEN
    RAISE EXCEPTION 'R4: a count changed (see the report above); nothing is kept';
  END IF;
  IF {left} THEN
    RAISE EXCEPTION 'R4: an object is still there (see the report above); nothing is kept';
  END IF;
  IF to_regrole('{role}') IS NOT NULL THEN  -- nested: AND does not short-circuit in SQL
    IF NOT ({readable}) THEN
      RAISE EXCEPTION 'R4: {role} cannot read a rebuilt env view (see the report above); nothing is kept';
    END IF;
  END IF;
END $r4$""",
    ]


class _Recorder:
    """A cursor stand-in that keeps the SQL ``_create_brokerage_views`` would run."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        if params is not None:
            raise ValueError("view DDL takes no parameters")
        self.statements.append(sql)


def view_statements(schema: str = "brokerage") -> List[str]:
    """Step 2: the env views as ``setup_fdw_foreign_tables`` builds them (this core's code)."""
    from bifrost_core.persistence.postgres.brokerage_views import _create_brokerage_views

    rec = _Recorder()
    _create_brokerage_views(rec, schema, env=True)
    return rec.statements


def forward_statements(env: str) -> List[str]:
    """Every statement of the drop transaction, without BEGIN / COMMIT."""
    parts: List[str] = ["SET LOCAL lock_timeout = '5s'", f"SET LOCAL ROLE {APP_ROLE}"]
    parts += guards(env)
    parts += ["DROP TABLE IF EXISTS pg_temp.r4_before", before_statement()]
    parts += [f"DROP VIEW IF EXISTS {v}" for v in COMPAT_VIEWS]
    parts += [s.strip() for s in view_statements()]
    parts.append(grant_statement(env))
    parts.append(f"DROP TABLE IF EXISTS {LEGACY_TABLE}")
    parts += report_statements(env)
    return parts


def reverse_statements(env: str) -> List[str]:
    """Every statement of the way back, without BEGIN / COMMIT. The table comes back empty."""
    db = env_db(env)
    parts: List[str] = ["SET LOCAL lock_timeout = '5s'", f"SET LOCAL ROLE {APP_ROLE}"]
    parts.append(
        f"""DO $r4$ BEGIN
  IF current_database() <> '{db}' THEN
    RAISE EXCEPTION 'R4 reverse: connected to %, but this SQL is for {db}', current_database();
  END IF;
END $r4$"""
    )
    parts.append(
        """DO $r4$ BEGIN
  IF to_regclass('public.trade') IS NULL OR to_regclass('public.trade_execution') IS NULL
     OR to_regclass('brokerage.trade_fill_splits') IS NULL THEN
    RAISE EXCEPTION 'R4 reverse: trade / trade_execution / brokerage.trade_fill_splits missing; nothing is created';
  END IF;
END $r4$"""
    )
    parts += [f"DROP VIEW IF EXISTS {v}" for v in COMPAT_VIEWS]
    parts += list(R3_COMPAT_VIEWS)
    parts += [f"DROP VIEW IF EXISTS {COMPAT_ENV_VIEW}", R3_ENV_VIEW]
    parts += list(LEGACY_TABLE_DDL)
    parts.append(
        "SELECT o AS restored, to_regclass(o) IS NOT NULL AS present FROM unnest(ARRAY["
        + ", ".join(f"'{o}'" for o in DROPPED)
        + "]) o"
    )
    return parts


def render(parts: List[str], *, commit: bool, title: str) -> str:
    body = ";\n\n".join(p.rstrip().rstrip(";") for p in parts)
    end = "COMMIT" if commit else "ROLLBACK"
    head = f"-- {title} ({'commit' if commit else 'dry run: ends ROLLBACK'})\n"
    return f"{head}BEGIN;\n\n{body};\n\n{end};\n"


def forward_sql(env: str, *, commit: bool = False) -> str:
    """The R4 drop for ``bifrost_<env>``, one transaction; ROLLBACK unless ``commit``."""
    return render(forward_statements(env), commit=commit, title=f"naming R4 drop, {env_db(env)}, core 0.47.0")


def reverse_sql(env: str, *, commit: bool = False) -> str:
    """The way back for ``bifrost_<env>`` (objects only; rows from the CSV with ``restore_command``)."""
    return render(
        reverse_statements(env), commit=commit, title=f"naming R4 reverse (objects), {env_db(env)}, core 0.47.0"
    )


EXPORT_SQL = (
    f"COPY (SELECT * FROM {LEGACY_TABLE} ORDER BY account_execution_instance_allocation_id) "
    "TO STDOUT WITH (FORMAT csv, HEADER)"
)
# Two statements, run one after the other (psql -c ... -c ...) after the reverse: the rows from
# the CSV on stdin, then the sequence past them.
RESTORE_SQL = f"COPY {LEGACY_TABLE} FROM STDIN WITH (FORMAT csv, HEADER)"
SEQUENCE_SQL = (
    f"SELECT setval(pg_get_serial_sequence('{LEGACY_TABLE}', 'account_execution_instance_allocation_id'), "
    f"(SELECT max(account_execution_instance_allocation_id) FROM {LEGACY_TABLE}))"
)

__all__ = [
    "APP_ROLE",
    "COMPAT_ENV_VIEW",
    "COMPAT_VIEWS",
    "DROPPED",
    "ENV_VIEWS",
    "ENVS",
    "EXPORT_SQL",
    "LEGACY_ROWS",
    "LEGACY_TABLE",
    "LEGACY_TABLE_DDL",
    "R3_COMPAT_VIEWS",
    "R3_ENV_VIEW",
    "REPORT",
    "RESTORE_SQL",
    "SEQUENCE_SQL",
    "forward_sql",
    "forward_statements",
    "render",
    "reverse_sql",
    "reverse_statements",
    "view_statements",
]
