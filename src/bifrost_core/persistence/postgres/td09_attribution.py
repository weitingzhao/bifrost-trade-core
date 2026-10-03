"""TD-09: move strategy attribution from Golden Source into each env (one-off, core 0.37.0).

Until core 0.37.0 a fill's trade was ``strategy_instance_id`` / ``strategy_opportunity_id``
on Golden Source's ``raw_broker.executions_raw_*`` rows -- one copy shared by DEV, STG and
PROD, whose instance ids are separate sequences -- plus per-env splits in
``account_execution_instance_allocation`` keyed by the view id. Each env now keeps its own
``strategy_instance_execution`` keyed by the fill (account_id, exec_id).

``migration_sql`` builds one transaction to run in an env database (``psql -d bifrost_<env>``).
It reads the old attribution through that env's FDW tables (``brokerage.executions_raw_*``
still carry the Golden Source columns), so it needs no Golden Source connection and no
credentials beyond the psql session. Steps, in order:

1. the table (``STRATEGY_INSTANCE_EXECUTION_DDL``, idempotent);
2. instance #3 moves to the account its three fills are on (Owner D3, 2026-10-03);
3. the table is emptied and reloaded -- whole fills from the rows the old view showed (a Flex
   row shadows its TWS twin), splits from the legacy split table;
4. optionally the env views (``--views``), so readers switch in the same transaction;
5. a report: rows, splits, per-instance counts. It ends ``ROLLBACK`` unless ``commit``.

The rules are the Owner-approved manifest (``REQUEST-td09-attribution-migration-2026-10-03``):
a fill goes to an env only when that env has the instance on the fill's account; for the ids
DEV and PROD both reused after the clone (158-162) and for any id past the manifest (> 162)
the stored opportunity must also be the instance's; the three instances deleted everywhere
(123, 124, 126) are dropped. Golden Source is not written.
"""

from __future__ import annotations

from typing import List, Optional

from bifrost_core.persistence.postgres.brokerage_tables import (
    INSTANCE_EXECUTION,
    LEGACY_INSTANCE_ALLOCATION,
    SCHEMA,
)
from bifrost_core.persistence.postgres.ddl import STRATEGY_INSTANCE_EXECUTION_DDL

# Instance ids DEV and PROD both used for different trades after the clone (manifest §3).
COLLIDED_IDS = (158, 159, 160, 161, 162)
# Highest id the manifest saw in DEV / PROD; a later id may collide too.
MANIFEST_MAX_ID = 162
# Deleted in every env; their fills are not migrated (Owner D4).
DANGLING_IDS = (123, 124, 126)
# Owner D3: instance #3's fills are all on U8829175; it was registered on U17123565.
INSTANCE_3_FIX = (3, "U17123565", "U8829175")


def _id_list(ids: tuple[int, ...]) -> str:
    return ", ".join(str(int(i)) for i in ids)


def _visible_attributed_rows(schema: str) -> str:
    """Golden Source rows carrying an instance, as the pre-0.37.0 ``executions`` view showed
    them: every Flex row, a TWS row only when no Flex row has its exec_id, every journal row."""
    return f"""
        SELECT account_id, exec_id, strategy_instance_id AS iid, strategy_opportunity_id AS opp
        FROM {schema}.executions_raw_flex
        WHERE strategy_instance_id IS NOT NULL
        UNION ALL
        SELECT t.account_id, t.exec_id, t.strategy_instance_id, t.strategy_opportunity_id
        FROM {schema}.executions_raw_tws t
        WHERE t.strategy_instance_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM {schema}.executions_raw_flex f
            WHERE f.exec_id = t.exec_id
              AND f.exec_id IS NOT NULL AND f.exec_id != ''
              AND t.exec_id IS NOT NULL AND t.exec_id != ''
          )
        UNION ALL
        SELECT account_id, exec_id, strategy_instance_id, strategy_opportunity_id
        FROM {schema}.executions_raw_journal
        WHERE strategy_instance_id IS NOT NULL
    """


def _raw_ids(schema: str) -> str:
    """Every raw row's view id (same encoding as the views) with its fill key."""
    return f"""
        SELECT executions_raw_flex_id AS account_executions_id, account_id, exec_id
        FROM {schema}.executions_raw_flex
        UNION ALL
        SELECT -(executions_raw_tws_id), account_id, exec_id FROM {schema}.executions_raw_tws
        UNION ALL
        SELECT -(1000000000 + executions_raw_journal_id), account_id, exec_id
        FROM {schema}.executions_raw_journal
    """


def load_statements(schema: str = SCHEMA) -> List[str]:
    """Steps 2-3: the #3 account fix, then empty and reload the table."""
    iid3, old_acct, new_acct = INSTANCE_3_FIX
    return [
        f"""
        UPDATE strategy_instance SET account_id = '{new_acct}', updated_at = now()
        WHERE strategy_instance_id = {iid3} AND account_id = '{old_acct}'
        """,
        f"DELETE FROM {INSTANCE_EXECUTION}",
        "DROP TABLE IF EXISTS pg_temp.td09_source",
        f"""
        CREATE TEMP TABLE td09_source ON COMMIT DROP AS
        SELECT DISTINCT account_id, exec_id, iid, opp FROM ({_visible_attributed_rows(schema)}) v
        """,
        f"""
        INSERT INTO {INSTANCE_EXECUTION} (account_id, exec_id, strategy_instance_id)
        SELECT s.account_id, s.exec_id, s.iid
        FROM td09_source s
        JOIN strategy_instance si
          ON si.strategy_instance_id = s.iid AND si.account_id = s.account_id
        WHERE COALESCE(s.exec_id, '') <> ''
          AND s.iid NOT IN ({_id_list(DANGLING_IDS)})
          AND (
            (s.iid NOT IN ({_id_list(COLLIDED_IDS)}) AND s.iid <= {MANIFEST_MAX_ID})
            OR s.opp IS NULL
            OR si.strategy_opportunity_id = s.opp
          )
        ON CONFLICT DO NOTHING
        """,
        # Splits: the legacy table is keyed by the view id; the fill key is that row's.
        # A split fill has no whole-fill row (the old writer cleared Golden Source's columns).
        "DROP TABLE IF EXISTS pg_temp.td09_splits",
        f"""
        CREATE TEMP TABLE td09_splits ON COMMIT DROP AS
        SELECT DISTINCT a.account_id, x.exec_id, a.strategy_instance_id, a.allocated_quantity
        FROM {LEGACY_INSTANCE_ALLOCATION} a
        JOIN ({_raw_ids(schema)}) x
          ON x.account_executions_id = a.account_executions_id AND x.account_id = a.account_id
        WHERE COALESCE(x.exec_id, '') <> ''
        """,
        f"""
        DELETE FROM {INSTANCE_EXECUTION} w
        USING td09_splits sp
        WHERE w.allocated_quantity IS NULL
          AND w.account_id = sp.account_id AND w.exec_id = sp.exec_id
        """,
        f"""
        INSERT INTO {INSTANCE_EXECUTION} (account_id, exec_id, strategy_instance_id, allocated_quantity)
        SELECT account_id, exec_id, strategy_instance_id, allocated_quantity FROM td09_splits
        """,
    ]


def report_statements() -> List[str]:
    """Step 5: what was loaded, and what was left behind (with why)."""
    return [
        f"""
        SELECT 'loaded' AS what, count(*) AS rows,
               count(*) FILTER (WHERE allocated_quantity IS NOT NULL) AS split_rows,
               count(DISTINCT strategy_instance_id) AS instances
        FROM {INSTANCE_EXECUTION}
        """,
        f"""
        SELECT 'per_instance' AS what, strategy_instance_id, count(*) AS rows
        FROM {INSTANCE_EXECUTION} GROUP BY strategy_instance_id ORDER BY strategy_instance_id
        """,
        f"""
        SELECT 'not_loaded' AS what, s.iid, s.account_id, s.exec_id,
               CASE
                 WHEN COALESCE(s.exec_id, '') = '' THEN 'no exec_id'
                 WHEN s.iid IN ({_id_list(DANGLING_IDS)}) THEN 'dangling (D4)'
                 WHEN si.strategy_instance_id IS NULL THEN 'instance not in this env'
                 WHEN si.account_id <> s.account_id THEN 'other account (D3)'
                 ELSE 'opportunity differs (collision)'
               END AS why
        FROM td09_source s
        LEFT JOIN strategy_instance si ON si.strategy_instance_id = s.iid
        WHERE NOT EXISTS (
            SELECT 1 FROM {INSTANCE_EXECUTION} w
            WHERE w.account_id = s.account_id AND w.exec_id = s.exec_id
        )
        ORDER BY s.iid, s.exec_id
        """,
        f"""
        SELECT 'past_manifest' AS what, s.iid, s.account_id, s.exec_id
        FROM td09_source s WHERE s.iid > {MANIFEST_MAX_ID} ORDER BY s.iid, s.exec_id
        """,
    ]


class _Recorder:
    """A cursor stand-in that keeps the SQL ``_create_brokerage_views`` would run."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        if params is not None:
            raise ValueError("view DDL takes no parameters")
        self.statements.append(sql)


def view_statements(schema: str = SCHEMA) -> List[str]:
    """Step 4: the env views as ``setup_fdw_foreign_tables`` builds them."""
    from bifrost_core.persistence.postgres.brokerage_ddl import _create_brokerage_views

    rec = _Recorder()
    _create_brokerage_views(rec, schema, env=True)
    return rec.statements


def migration_sql(
    *, commit: bool = False, views: bool = False, schema: str = SCHEMA, role: Optional[str] = "bifrost"
) -> str:
    """The whole transaction for one env database, ending ROLLBACK unless ``commit``.

    ``role``: run as the app role (``SET LOCAL ROLE``) so the table and views it creates
    belong to it, as db-init's would, when psql connects as a superuser. None skips it."""
    parts: List[str] = ["BEGIN", "SET LOCAL lock_timeout = '10s'"]
    if role:
        if not role.replace("_", "").isalnum():
            raise ValueError(f"not a role name: {role!r}")
        parts.append(f"SET LOCAL ROLE {role}")
    parts += [s.strip() for s in STRATEGY_INSTANCE_EXECUTION_DDL]
    parts += [s.strip() for s in load_statements(schema)]
    if views:
        parts += [s.strip() for s in view_statements(schema)]
    parts += [s.strip() for s in report_statements()]
    parts.append("COMMIT" if commit else "ROLLBACK")
    return ";\n\n".join(p.rstrip().rstrip(";") for p in parts) + ";\n"


__all__ = [
    "COLLIDED_IDS",
    "DANGLING_IDS",
    "INSTANCE_3_FIX",
    "MANIFEST_MAX_ID",
    "load_statements",
    "migration_sql",
    "report_statements",
    "view_statements",
]
