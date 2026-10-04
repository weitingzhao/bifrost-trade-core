"""TD-74 against real Postgres: core works with and without ``settings.flex_*_range_days``.

DEV / STG / PROD have both columns (NOT NULL, defaults 30 / 360) when this core is delivered;
the Owner drops them afterwards (infra db-step ``2026-10-10-td74-drop-settings-flex-columns``).
So each case runs on the fresh schema (no columns) and on the schema the databases have today
(columns added back here): db-init neither fails on the old one nor adds them to the new one,
and the settings reader and writers work on both.

Marked ``db`` (``make test-db``). Account ids are made up.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator

import pytest

from bifrost_core.monitor.reader import settings
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.persistence.postgres.ddl import _ensure_tables
from bifrost_core.persistence.postgres.wave13_migrations import migrate_wave13_reconcile_legacy_schema

pytestmark = pytest.mark.db

CFG = {"sink": "postgres"}
COLUMNS = (("flex_default_range_days", 30), ("flex_init_range_days", 360))


def _present(conn: Any) -> Dict[str, bool]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'settings' AND column_name = ANY(%s)",
            ([c for c, _ in COLUMNS],),
        )
        found = {r[0] for r in cur.fetchall()}
    return {c: c in found for c, _ in COLUMNS}


def _drop_committed(conn: Any) -> None:
    """What the Owner's db-step leaves. ``_ensure_tables`` commits, so this outlives a test."""
    conn.rollback()
    with conn.cursor() as cur:
        for column, _ in COLUMNS:
            cur.execute(f"ALTER TABLE settings DROP COLUMN IF EXISTS {column}")
    conn.commit()


class _NoClose:
    """The fixture's connection, which the settings writers must not close."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def cursor(self, **kw: Any) -> Any:
        return self._conn.cursor(**kw)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        return None


@pytest.fixture(params=["dropped", "still_there"])
def db(request, pg_conn, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    _drop_committed(pg_conn)
    if request.param == "still_there":
        with pg_conn.cursor() as cur:
            for column, default in COLUMNS:
                cur.execute(f"ALTER TABLE settings ADD COLUMN {column} integer NOT NULL DEFAULT {default}")
        pg_conn.commit()
    _ensure_tables(pg_conn)  # db-init on that database: no error, and nothing added or removed
    migrate_wave13_reconcile_legacy_schema(pg_conn.cursor())
    pg_conn.commit()
    assert all(_present(pg_conn).values()) is (request.param == "still_there")
    assert any(_present(pg_conn).values()) is (request.param == "still_there")
    monkeypatch.setattr(ws, "open_conn", lambda _cfg: _NoClose(pg_conn))
    try:
        yield pg_conn
    finally:
        _drop_committed(pg_conn)


def test_db_init_never_adds_the_columns_back(pg_conn) -> None:
    _drop_committed(pg_conn)
    _ensure_tables(pg_conn)
    _ensure_tables(pg_conn)
    assert not any(_present(pg_conn).values())


def test_the_settings_row_reads_and_writes_on_either_schema(db) -> None:
    assert settings.write_ib_config(CFG, "U0000001", "U0000002", "U0000003") is True
    got = settings.get_ib_config(db)
    assert got == {
        "ib_host_account_id": "U0000001",
        "stream_host_account_id": "U0000002",
        "stream_secondary_account_id": "U0000003",
    }
    assert settings.write_ib_config(CFG, None, None, None) is True
    assert settings.get_ib_config(db) == {
        "ib_host_account_id": None,
        "stream_host_account_id": None,
        "stream_secondary_account_id": None,
    }
