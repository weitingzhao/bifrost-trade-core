"""db-init's view rebuild keeps the grants it did not make (TD-85 D2 follow-up).

``ensure_brokerage_schema`` drops and recreates ``raw_broker.executions`` / ``_final`` / ``_fly``
on every run. Before core 0.48.2 a grant made by hand went with the old view: Research's
``analytics_writer`` lost SELECT on ``executions_final`` and its memory distill failed on
2026-10-05. Marked ``db``; everything runs in the fixture's transaction and is rolled back
(the role too). Role names are made up.
"""

from __future__ import annotations

from typing import Any

import pytest

from bifrost_core.persistence.postgres.brokerage_ddl import ensure_brokerage_schema

pytestmark = pytest.mark.db

READER = "td85_view_grant_reader"


class _Savepointed:
    """The fixture's connection with commit / rollback mapped onto one savepoint."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._run("SAVEPOINT view_grants")

    def _run(self, sql: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql)

    def cursor(self, **kw: Any) -> Any:
        return self._conn.cursor(**kw)

    def commit(self) -> None:
        self._run("RELEASE SAVEPOINT view_grants")
        self._run("SAVEPOINT view_grants")


@pytest.fixture
def db(pg_conn) -> Any:
    conn = _Savepointed(pg_conn)
    ensure_brokerage_schema(conn, log=lambda m: None)
    yield conn
    pg_conn.rollback()


def _can(db: Any, role: str, rel: str, privilege: str = "SELECT") -> bool:
    with db.cursor() as cur:
        cur.execute("SELECT has_table_privilege(%s, %s, %s)", (role, rel, privilege))
        return cur.fetchone()[0]


def test_a_hand_grant_survives_the_rebuild(db: Any) -> None:
    with db.cursor() as cur:
        cur.execute(f"CREATE ROLE {READER} NOLOGIN")
        cur.execute(f"GRANT USAGE ON SCHEMA raw_broker TO {READER}")
        cur.execute(f"GRANT SELECT ON raw_broker.executions_final TO {READER}")
    assert _can(db, READER, "raw_broker.executions_final")

    ensure_brokerage_schema(db, log=lambda m: None)

    assert _can(db, READER, "raw_broker.executions_final")
    # Only what was granted comes back: no other view, no other privilege.
    assert not _can(db, READER, "raw_broker.executions")
    assert not _can(db, READER, "raw_broker.executions_fly")
    assert not _can(db, READER, "raw_broker.executions_final", "INSERT")


def test_the_rebuild_still_recreates_the_views(db: Any) -> None:
    with db.cursor() as cur:
        cur.execute(
            "SELECT oid FROM pg_class WHERE oid = 'raw_broker.executions_final'::regclass"
        )
        before = cur.fetchone()[0]
    ensure_brokerage_schema(db, log=lambda m: None)
    with db.cursor() as cur:
        cur.execute("SELECT 'raw_broker.executions_final'::regclass::oid")
        after = cur.fetchone()[0]
    assert after != before
