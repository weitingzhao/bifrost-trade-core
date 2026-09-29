"""trade_review against real Postgres: the DDL and the upsert rules.

Marked `db`: `make test` skips it, `make test-all` with PGHOST runs it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from bifrost_core.monitor.reader import trade_review

pytestmark = pytest.mark.db


@pytest.fixture
def reviews(pg_conn, monkeypatch: pytest.MonkeyPatch):
    """The review functions, writing through the fixture's connection (rolled back at teardown)."""

    class _Shared:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        def cursor(self, **kw: Any) -> Any:
            return self._conn.cursor(**kw)

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(trade_review, "_conn_from_config", lambda _cfg: _Shared(pg_conn))
    return trade_review


def _instance(pg_conn) -> int:
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO strategy_structure (name) VALUES ('test-review-structure') RETURNING strategy_structure_id")
        structure_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO strategy_opportunity (name, strategy_structure_id) VALUES ('test-review-opp', %s) "
            "RETURNING strategy_opportunity_id",
            (structure_id,),
        )
        opportunity_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO strategy_instance (strategy_opportunity_id, account_id, opened_at) "
            "VALUES (%s, 'TEST-REVIEW', %s) RETURNING strategy_instance_id",
            (opportunity_id, datetime(2026, 1, 5, tzinfo=timezone.utc)),
        )
        return cur.fetchone()[0]


def test_the_table_exists_with_one_row_per_instance(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'trade_review' AND constraint_type = 'UNIQUE'
            """
        )
        assert cur.fetchone() is not None


def test_a_review_is_written_kept_confirmed_and_reopened(reviews, pg_conn) -> None:
    cfg = {"sink": "postgres"}
    inst = _instance(pg_conn)

    row = reviews.save_review(cfg, inst, {"tags_added": ["late exit"], "tags_dropped": ["held_to_expiry"]})
    assert row["tags_added"] == ["late exit"] and row["reviewed"] is False

    # A field left out keeps what is stored.
    row = reviews.save_review(cfg, inst, {"reviewed": True})
    assert row["tags_added"] == ["late exit"] and row["tags_dropped"] == ["held_to_expiry"]
    stamped = row["reviewed_at"]
    assert stamped is not None

    # A second confirm keeps the first stamp.
    assert reviews.save_review(cfg, inst, {"reviewed": True})["reviewed_at"] == stamped

    # Reopening clears the stamp and keeps the tags.
    row = reviews.save_review(cfg, inst, {"reviewed": False})
    assert row["reviewed"] is False and row["tags_added"] == ["late exit"]

    assert [r["strategy_instance_id"] for r in reviews.list_reviews(cfg)].count(inst) == 1


def test_a_review_for_an_instance_that_does_not_exist_is_refused(reviews) -> None:
    import psycopg2

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        reviews.save_review({"sink": "postgres"}, 999_999_999, {"reviewed": True})
