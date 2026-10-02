"""TD-15 write outcomes: the exception family and the shared PATCH input rules."""

from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg2
import psycopg2.errors
import pytest

from bifrost_core.monitor.reader import errors
from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import (
    ReadFailed,
    WriteConflict,
    WriteError,
    WriteFailed,
    WriteInvalid,
    WriteNotFound,
)
from write_fakes import FakeConn, Reply


# --- the family -------------------------------------------------------------------


def test_outcomes_share_a_base_and_carry_a_reason() -> None:
    for cls in (WriteNotFound, WriteConflict, WriteInvalid, WriteFailed):
        err = cls("Because.")
        assert isinstance(err, WriteError)
        assert err.reason == "Because." == str(err)
        assert cls().reason  # a default sentence, never empty


def test_outcomes_keep_the_builtin_bases_old_excepts_rely_on() -> None:
    assert issubclass(WriteNotFound, LookupError)
    assert issubclass(WriteInvalid, ValueError)
    assert issubclass(WriteFailed, RuntimeError)
    assert not issubclass(WriteConflict, ValueError)
    assert not issubclass(ReadFailed, WriteError)


def test_the_outcomes_are_exported_from_the_package() -> None:
    from bifrost_core.monitor import reader

    for name in ("ReadFailed", "WriteError", "WriteNotFound", "WriteConflict", "WriteInvalid", "WriteFailed"):
        assert getattr(reader, name) is getattr(errors, name)


# --- the PATCH envelope -------------------------------------------------------------


def test_check_fields_refuses_empty_unknown_and_non_objects() -> None:
    with pytest.raises(WriteInvalid, match="Nothing to change"):
        ws.check_fields({}, ("name",), "thing")
    with pytest.raises(WriteInvalid, match="Unknown thing field: colour"):
        ws.check_fields({"name": "a", "colour": "red"}, ("name",), "thing")
    with pytest.raises(WriteInvalid, match="must be an object"):
        ws.check_fields(["name"], ("name",), "thing")
    assert ws.check_fields({"name": None}, ("name",), "thing") == {"name": None}


def test_text_trims_clears_on_null_and_never_turns_blank_into_null() -> None:
    assert ws.text("  Roll  ", "label", nullable=True) == "Roll"
    assert ws.text(None, "label", nullable=True) is None
    with pytest.raises(WriteInvalid, match="send null to clear"):
        ws.text("   ", "label", nullable=True)
    with pytest.raises(WriteInvalid, match="name is required"):
        ws.text("", "name", nullable=False)
    with pytest.raises(WriteInvalid, match="name is required"):
        ws.text(None, "name", nullable=False)
    with pytest.raises(WriteInvalid, match="must be text"):
        ws.text(5, "name", nullable=False)


def test_numbers_are_type_checked_not_coerced_from_junk() -> None:
    assert ws.integer("12", "qty", nullable=False) == 12
    assert ws.integer(3.0, "qty", nullable=False) == 3
    for junk in (True, 1.5, "1x", [1]):
        with pytest.raises(WriteInvalid):
            ws.integer(junk, "qty", nullable=False)
    with pytest.raises(WriteInvalid, match="1 or more"):
        ws.integer(0, "qty", nullable=False, minimum=1)
    assert ws.number(2, "price", nullable=True) == 2.0
    for junk in (True, "2", float("nan")):
        with pytest.raises(WriteInvalid):
            ws.number(junk, "price", nullable=True)
    with pytest.raises(WriteInvalid):
        ws.boolean(None, "is_active")
    with pytest.raises(WriteInvalid):
        ws.boolean(1, "is_active")


def test_timestamps_and_dates() -> None:
    assert ws.timestamp(0, "t", nullable=False) == datetime(1970, 1, 1, tzinfo=timezone.utc)
    assert ws.timestamp("2026-10-02T15:00:00Z", "t", nullable=False) == datetime(2026, 10, 2, 15, tzinfo=timezone.utc)
    naive = ws.timestamp(datetime(2026, 1, 1), "t", nullable=False)
    assert naive is not None and naive.tzinfo is timezone.utc
    assert ws.timestamp(None, "t", nullable=True) is None
    with pytest.raises(WriteInvalid):
        ws.timestamp("soon", "t", nullable=False)
    with pytest.raises(WriteInvalid):
        ws.timestamp(None, "t", nullable=False)
    assert ws.calendar_date("2026-11-20", "exit_by", nullable=True) == date(2026, 11, 20)
    with pytest.raises(WriteInvalid):
        ws.calendar_date("20/11/2026", "exit_by", nullable=True)


def test_list_value_refuses_null() -> None:
    assert ws.list_value([], "symbols") == []
    with pytest.raises(WriteInvalid, match=r"send \[\] to empty it"):
        ws.list_value(None, "symbols")


# --- connections and transactions ---------------------------------------------------


def test_write_connection_without_postgres_is_write_failed() -> None:
    with pytest.raises(WriteFailed, match="not configured"):
        with ws.write_connection(None, "thing"):
            pass
    with pytest.raises(WriteFailed, match="not configured"):
        with ws.write_connection({"sink": "redis"}, "thing"):
            pass


def test_write_connection_unreachable_is_write_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(params, golden=False):
        raise psycopg2.OperationalError("connection refused")

    monkeypatch.setattr(ws, "connect", refuse)
    with pytest.raises(WriteFailed, match="unreachable"):
        with ws.write_connection({"sink": "postgres"}, "thing"):
            pass
    with pytest.raises(WriteFailed, match="Golden Source is unreachable"):
        with ws.write_connection({"sink": "postgres"}, "thing", golden=True):
            pass


def test_write_connection_closes_what_it_opened_and_leaves_a_live_one_open(monkeypatch: pytest.MonkeyPatch) -> None:
    opened = FakeConn()
    monkeypatch.setattr(ws, "connect", lambda params, golden=False: opened)
    with ws.write_connection({"sink": "postgres"}, "thing") as conn:
        assert conn is opened
    assert opened.closed
    live = FakeConn()
    with ws.write_connection(live, "thing") as conn:
        assert conn is live
    assert not live.closed


def test_write_transaction_commits_or_rolls_back_into_an_outcome() -> None:
    conn = FakeConn()
    with ws.write_transaction(conn, "thing"):
        pass
    assert conn.commits == 1 and conn.rollbacks == 0

    conn = FakeConn([("UPDATE", Reply(raises=psycopg2.OperationalError("server closed the connection")))])
    with pytest.raises(WriteFailed, match="database write failed"):
        with ws.write_transaction(conn, "thing"):
            with conn.cursor() as cur:
                cur.execute("UPDATE t SET a = 1")
    assert conn.commits == 0 and conn.rollbacks == 1

    conn = FakeConn()
    with pytest.raises(WriteNotFound):
        with ws.write_transaction(conn, "thing"):
            raise WriteNotFound("No thing 7.")
    assert conn.rollbacks == 1


@pytest.mark.parametrize(
    ("exc", "on_fk", "outcome"),
    [
        (psycopg2.errors.UniqueViolation("dup"), "invalid", WriteConflict),
        (psycopg2.errors.ForeignKeyViolation("fk"), "invalid", WriteInvalid),
        (psycopg2.errors.ForeignKeyViolation("fk"), "conflict", WriteConflict),
        (psycopg2.errors.ForeignKeyViolation("fk"), "not_found", WriteNotFound),
        (psycopg2.errors.NotNullViolation("nn"), "invalid", WriteInvalid),
        (psycopg2.errors.CheckViolation("ck"), "invalid", WriteInvalid),
        (psycopg2.errors.InvalidTextRepresentation("bad"), "invalid", WriteInvalid),
        (psycopg2.errors.QueryCanceled("timeout"), "invalid", WriteFailed),
        (RuntimeError("anything else"), "invalid", WriteFailed),
    ],
)
def test_database_errors_map_to_outcomes(exc: BaseException, on_fk: str, outcome: type) -> None:
    assert isinstance(ws.as_write_error(exc, "thing", on_fk=on_fk), outcome)


def test_name_list_and_plural() -> None:
    assert ws.name_list(["A"]) == "A"
    assert ws.name_list(["A", "B", "C"]) == "A, B and C"
    assert ws.name_list([str(i) for i in range(7)], limit=3) == "0, 1, 2 and 4 more"
    assert ws.plural(1, "execution is", "executions are") == "1 execution is"
    assert ws.plural(3, "execution is", "executions are") == "3 executions are"
