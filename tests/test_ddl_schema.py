"""DDL integration: ensure _ensure_tables creates expected core tables."""

from __future__ import annotations

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
              AND column_name = 'params_json'
            """
        )
        assert cur.fetchone() is not None
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'gate_safety_strategy'
              AND column_name = 'epsilon_band'
            """
        )
        assert cur.fetchone() is None
