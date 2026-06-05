"""DDL integration: ensure _ensure_tables creates expected core tables."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.db


def test_ddl_creates_daemon_run_status(pg_conn):
    """Smoke: daemon_run_status exists after _ensure_tables."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = 'daemon_run_status'
            """
        )
        assert cur.fetchone() is not None


@pytest.fixture
def pg_conn():
    """PostgreSQL connection from env or skip."""
    if not os.environ.get("PGHOST") and not os.environ.get("BIFROST_TEST_DB"):
        pytest.skip("Set PGHOST or BIFROST_TEST_DB=1 for db tests")
    import psycopg2
    import yaml
    from pathlib import Path

    from bifrost_core.persistence.postgres.connection import _get_conn_params
    from bifrost_core.persistence.postgres.ddl import _ensure_tables

    root = Path(__file__).resolve().parents[1]
    cfg_path = root / "config" / "config.yaml.example"
    with open(cfg_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    params = _get_conn_params(config)
    conn = psycopg2.connect(**params)
    _ensure_tables(conn)
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()
