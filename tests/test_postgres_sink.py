"""StatusSink PostgreSQL implementation smoke tests."""

from __future__ import annotations


from bifrost_core.persistence.status_sink import StatusSink
from bifrost_core.persistence.postgres.postgres_sink import PostgreSQLSink


def test_postgres_sink_implements_status_sink():
    assert issubclass(PostgreSQLSink, StatusSink)


def test_postgres_sink_has_write_snapshot():
    assert hasattr(PostgreSQLSink, "write_snapshot")
