"""DDL integration: ensure _ensure_tables creates expected core tables."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.db


def test_ddl_creates_settings(pg_conn):
    """Smoke: settings exists after _ensure_tables (daemon IPC tables are Redis-only)."""
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = current_schema() AND table_name = 'settings'
            """
        )
        assert cur.fetchone() is not None


def test_ddl_does_not_create_daemon_ipc_tables(pg_conn):
    """Daemon IPC tables must not be recreated in public (migrated to Redis)."""
    retired = (
        "daemon_heartbeat",
        "daemon_run_status",
        "daemon_control",
        "daemon_auto_status_current",
        "daemon_auto_status_history",
        "daemon_auto_operations",
        "account_sync_heartbeat",
        "account_sync_run_status",
        "account_sync_control",
    )
    with pg_conn.cursor() as cur:
        for name in retired:
            cur.execute(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_name = %s
                """,
                (name,),
            )
            assert cur.fetchone() is None, name


def test_ddl_does_not_create_retired_gate_safety_children(pg_conn):
    """1:1 gate_safety child tables are merged into gate_safety_strategy."""
    retired = ("gate_safety_state", "gate_safety_intent", "gate_safety_guard")
    with pg_conn.cursor() as cur:
        for name in retired:
            cur.execute(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_name = %s
                """,
                (name,),
            )
            assert cur.fetchone() is None, name
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'gate_safety_strategy'
              AND column_name = 'epsilon_band'
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
