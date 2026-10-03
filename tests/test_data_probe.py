"""data_probe (D8-A): what the Ops platform reads instead of naming Trade tables.

The unit tests script the database; the ``db`` tests run the FK closure on the real
schema, where ``trades`` must hold every table ``TRUNCATE strategy_instance CASCADE``
would empty.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bifrost_core.monitor.reader import data_probe
from bifrost_core.monitor.reader.errors import ReadFailed
from bifrost_core.monitor.reader.common import StatusReader
from write_fakes import FakeConn, Reply

STAMP = datetime(2031, 3, 4, 14, 30, tzinfo=timezone.utc)


def _scripted(missing: str = "") -> FakeConn:
    rules = []
    if missing:
        rules.append(("SELECT to_regclass(%s) IS NOT NULL", Reply(one=(False,), once=True)))
    rules += [
        ("SELECT to_regclass(%s) IS NOT NULL", Reply(one=(True,))),
        ("SELECT max(", Reply(one=(STAMP,))),
        ("SELECT count(*) FROM strategy_instance", Reply(one=(12,))),
        ("WITH RECURSIVE closure", Reply(all=[("strategy_instance",), ("strategy_plan",), ("trade_review",)])),
    ]
    return FakeConn(rules)


def test_the_probe_answers_by_role() -> None:
    out = data_probe.read_data_probe(_scripted())
    assert out["generated_at"].endswith("Z")
    assert out["activity"] == [
        {"source": "trades", "last_ts": "2031-03-04T14:30:00Z"},
        {"source": "opportunities", "last_ts": "2031-03-04T14:30:00Z"},
        {"source": "watchlist", "last_ts": "2031-03-04T14:30:00Z"},
    ]
    assert out["sample"] == {"label": "trades", "rows": 12}
    trades = out["clone_groups"][0]
    assert trades["name"] == "trades"
    # the seed first, then what references it
    assert trades["tables"] == ["strategy_instance", "strategy_plan", "trade_review"]
    assert [g["name"] for g in out["clone_groups"]] == ["trades", "rules", "position_categories", "watchlist"]


def test_a_missing_source_is_reported_not_dropped() -> None:
    out = data_probe.read_data_probe(_scripted(missing="first"))
    assert out["activity"][0] == {"source": "trades", "last_ts": None, "detail": "missing"}
    assert len(out["activity"]) == 3


def test_the_reader_turns_a_failed_read_into_read_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = StatusReader.__new__(StatusReader)
    monkeypatch.setattr(StatusReader, "_connect", lambda self: False)
    with pytest.raises(ReadFailed, match="data_probe: database unavailable"):
        reader.get_data_probe()


# --- real Postgres -------------------------------------------------------------------------


@pytest.mark.db
def test_the_trades_group_is_what_truncate_cascade_would_empty(pg_conn) -> None:
    out = data_probe.read_data_probe(pg_conn)
    groups = {g["name"]: g for g in out["clone_groups"]}
    trades = groups["trades"]["tables"]
    assert trades[0] == "strategy_instance"
    for child in ("strategy_instance_execution", "strategy_plan", "trade_review"):
        assert child in trades
    # The opportunity group holds the trades group: a trade references its opportunity.
    assert set(trades) <= set(groups["rules"]["tables"])
    assert groups["watchlist"]["tables"][0] == "watchlist"
    assert isinstance(out["sample"]["rows"], int)
    assert {a["source"] for a in out["activity"]} == {"trades", "opportunities", "watchlist"}
