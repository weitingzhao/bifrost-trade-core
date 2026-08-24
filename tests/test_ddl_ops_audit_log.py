"""Wave 4: ops_audit_log partition helpers + (optional) DB upgrade path."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from bifrost_core.persistence.postgres.ddl import (
    _add_months,
    _ops_audit_partition_name,
    _month_start,
    drop_ops_audit_log_partitions_older_than,
)


def test_ops_audit_partition_name():
    assert _ops_audit_partition_name(2026, 8) == "ops_audit_log_y2026m08"
    assert _ops_audit_partition_name(2025, 12) == "ops_audit_log_y2025m12"


def test_add_months_wraps_year():
    assert _add_months(date(2026, 11, 1), 2) == date(2027, 1, 1)
    assert _add_months(date(2026, 3, 1), -3) == date(2025, 12, 1)
    assert _month_start(date(2026, 8, 24)) == date(2026, 8, 1)


def test_drop_ops_audit_log_skips_when_not_partitioned():
    cur = MagicMock()
    # _ops_audit_log_is_partitioned → fetchone returns (False,)
    cur.fetchone.return_value = (False,)
    assert drop_ops_audit_log_partitions_older_than(cur, cutoff_months=3) == 0


def test_drop_ops_audit_log_drops_old_partitions(monkeypatch):
    class _FakeDate(date):
        @classmethod
        def today(cls):
            return date(2026, 8, 15)

    monkeypatch.setattr(
        "bifrost_core.persistence.postgres.ddl.date",
        _FakeDate,
    )

    cur = MagicMock()
    # First fetchone: is_partitioned → True
    cur.fetchone.return_value = (True,)
    cur.fetchall.return_value = [
        ("ops_audit_log_y2026m05",),  # keep (within 3 months of Aug → May+)
        ("ops_audit_log_y2026m04",),  # drop (Apr < May cutoff)
        ("ops_audit_log_y2025m12",),  # drop
        ("ops_audit_log_y2026m08",),  # keep
    ]

    dropped = drop_ops_audit_log_partitions_older_than(cur, cutoff_months=3)
    assert dropped == 2
    drop_sqls = [c.args[0] for c in cur.execute.call_args_list if "DROP TABLE" in str(c.args[0])]
    assert any("ops_audit_log_y2026m04" in s for s in drop_sqls)
    assert any("ops_audit_log_y2025m12" in s for s in drop_sqls)
    assert not any("ops_audit_log_y2026m05" in s for s in drop_sqls)


@pytest.mark.db
def test_upgrade_ops_audit_log_fresh_and_idempotent(pg_conn):
    """Fresh create is partitioned timestamptz; re-run is idempotent."""
    from bifrost_core.persistence.postgres.ddl import (
        _ops_audit_log_is_partitioned,
        _ops_audit_log_timestamp_is_timestamptz,
        _upgrade_ops_audit_log_to_partitioned,
    )

    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ops_audit_log CASCADE")
        cur.execute("DROP TABLE IF EXISTS ops_audit_log_legacy CASCADE")
        _upgrade_ops_audit_log_to_partitioned(cur)
        assert _ops_audit_log_is_partitioned(cur)
        assert _ops_audit_log_timestamp_is_timestamptz(cur)
        _upgrade_ops_audit_log_to_partitioned(cur)  # idempotent
        assert _ops_audit_log_is_partitioned(cur)
    pg_conn.commit()


@pytest.mark.db
def test_upgrade_ops_audit_log_from_heap_double(pg_conn):
    """Heap double-precision table migrates to partitioned timestamptz with rows."""
    from bifrost_core.persistence.postgres.ddl import (
        _ops_audit_log_is_partitioned,
        _ops_audit_log_timestamp_is_timestamptz,
        _upgrade_ops_audit_log_to_partitioned,
    )

    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ops_audit_log CASCADE")
        cur.execute("DROP TABLE IF EXISTS ops_audit_log_legacy CASCADE")
        cur.execute(
            """
            CREATE TABLE ops_audit_log (
                id          BIGSERIAL PRIMARY KEY,
                timestamp   DOUBLE PRECISION NOT NULL DEFAULT EXTRACT(EPOCH FROM NOW()),
                operator    TEXT NOT NULL DEFAULT 'unknown',
                source_ip   TEXT,
                action      TEXT NOT NULL,
                target      TEXT NOT NULL,
                command_id  TEXT,
                outcome     TEXT NOT NULL,
                detail      TEXT,
                request_id  TEXT
            )
            """
        )
        cur.execute(
            """
            INSERT INTO ops_audit_log (timestamp, operator, action, target, outcome)
            VALUES
              (1719792000.0, 'op1', 'restart', 'api', 'ok'),
              (1722470400.0, 'op2', 'scale', 'daemon', 'fail'),
              (EXTRACT(EPOCH FROM NOW()), 'op3', 'sync', 'git', 'ok')
            """
        )
        _upgrade_ops_audit_log_to_partitioned(cur)
        assert _ops_audit_log_is_partitioned(cur)
        assert _ops_audit_log_timestamp_is_timestamptz(cur)
        cur.execute("SELECT COUNT(*) FROM ops_audit_log")
        assert cur.fetchone()[0] == 3
        cur.execute("SELECT to_regclass('public.ops_audit_log_legacy')")
        assert cur.fetchone()[0] is None
    pg_conn.commit()


@pytest.fixture
def pg_conn():
    """PostgreSQL connection from env or skip."""
    import os

    if not os.environ.get("PGHOST") and not os.environ.get("BIFROST_TEST_DB"):
        pytest.skip("Set PGHOST or BIFROST_TEST_DB=1 for db tests")
    import psycopg2
    import yaml
    from pathlib import Path

    cfg_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        pg = cfg.get("postgres") or {}
    else:
        pg = {}
    conn = psycopg2.connect(
        host=os.environ.get("PGHOST", pg.get("host", "127.0.0.1")),
        port=int(os.environ.get("PGPORT", pg.get("port", 5432))),
        dbname=os.environ.get("PGDATABASE", pg.get("database", "bifrost_dev")),
        user=os.environ.get("PGUSER", pg.get("user", "bifrost")),
        password=os.environ.get("PGPASSWORD", pg.get("password", "")),
    )
    yield conn
    conn.close()
