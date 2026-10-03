"""preference_instrument_class against real Postgres: the DDL, the upsert, the CHECK.

Marked `db`: `make test` skips it, `make test-all` with PGHOST runs it.
"""

from __future__ import annotations

import pytest

from bifrost_core.portfolio.reader import instrument_class as ic

pytestmark = pytest.mark.db


class _NoCommit:
    """The fixture's connection, with commit / rollback left to its teardown."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, **kw):
        return self._conn.cursor(**kw)

    def commit(self):
        return None

    def rollback(self):
        return None


def test_register_change_and_drop(pg_conn):
    conn = _NoCommit(pg_conn)
    # Invented keys (fixtures are never copied from DEV).
    assert ic.set_instrument_class(conn, "ZZFI", "fixed_income", note="bond fund") == (True, None)
    assert ic.set_instrument_class(conn, "ZZFI", "cash_like") == (True, None)
    rows = {r["contract_key"]: r for r in ic.list_instrument_classes(conn)}
    # A change keeps the note it was not given.
    assert rows["ZZFI"]["instrument_class"] == "cash_like"
    assert rows["ZZFI"]["note"] == "bond fund"
    assert ic.delete_instrument_class(conn, "ZZFI") is True
    assert "ZZFI" not in {r["contract_key"] for r in ic.list_instrument_classes(conn)}
    pg_conn.rollback()


def test_a_full_replace_clears_the_note_it_was_not_given(pg_conn):
    conn = _NoCommit(pg_conn)
    assert ic.set_instrument_class(conn, "ZZFR", "fixed_income", note="bond fund") == (True, None)
    # TD-15: PUT /instrument-classes is a full replace -- no note sent, no note kept.
    assert ic.set_instrument_class(conn, "ZZFR", "cash_like", keep_note=False) == (True, None)
    row = {r["contract_key"]: r for r in ic.list_instrument_classes(conn)}["ZZFR"]
    assert (row["instrument_class"], row["note"]) == ("cash_like", None)
    assert ic.set_instrument_class(conn, "ZZFR", "stock", note="core", keep_note=False) == (True, None)
    row = {r["contract_key"]: r for r in ic.list_instrument_classes(conn)}["ZZFR"]
    assert (row["instrument_class"], row["note"]) == ("stock", "core")
    pg_conn.rollback()


def test_the_table_refuses_a_fourth_class(pg_conn):
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO preference_instrument_class (contract_key, instrument_class) VALUES ('ZZBAD', 'bond')"
            )
    pg_conn.rollback()
