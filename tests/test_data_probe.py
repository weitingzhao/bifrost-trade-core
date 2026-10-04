"""data_probe (D8-A): what the Ops platform reads instead of naming Trade tables.

The unit tests script the database; the ``db`` tests run the FK closure on the real
schema, where ``trades`` must hold every table ``TRUNCATE trade CASCADE`` would empty
(``strategy_instance`` before naming R3; a compatibility view of that name is never a seed).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bifrost_core.monitor.reader import data_probe
from bifrost_core.monitor.reader.errors import ReadFailed
from bifrost_core.monitor.reader.common import StatusReader
from write_fakes import FakeConn, Reply

STAMP = datetime(2031, 3, 4, 14, 30, tzinfo=timezone.utc)


IS_TABLE = "SELECT EXISTS (SELECT 1 FROM pg_class WHERE oid = to_regclass(%s)"


def _scripted(missing: int = 0) -> FakeConn:
    """``missing``: how many of the first table lookups answer "no such table"."""
    rules = [(IS_TABLE, Reply(one=(False,), once=True)) for _ in range(missing)]
    rules += [
        (IS_TABLE, Reply(one=(True,))),
        ("SELECT max(", Reply(one=(STAMP,))),
        ("SELECT count(*) FROM trade", Reply(one=(12,))),
        ("WITH RECURSIVE closure", Reply(all=[("strategy_plan",), ("trade",), ("trade_execution",), ("trade_review",)])),
        ("SELECT DISTINCT upper(trim(symbol))", Reply(all=[("QQAA",), ("QQBB",)])),
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
    assert trades["tables"] == ["trade", "strategy_plan", "trade_execution", "trade_review"]
    assert [g["name"] for g in out["clone_groups"]] == ["trades", "rules", "position_categories", "watchlist"]
    assert out["watchlist"] == {"label": "optionable_stocks", "symbols": ["QQAA", "QQBB"], "count": 2}


def test_the_watchlist_filter_is_the_platforms_old_select() -> None:
    conn = _scripted()
    data_probe.read_data_probe(conn)
    sql = next(text for text, _ in conn.executed if "upper(trim(symbol))" in text)
    for clause in ("FROM watchlist", "sec_type = 'STK'", "optionable = true", "symbol IS NOT NULL", "trim(symbol) <> ''"):
        assert clause in sql
    assert "ORDER BY 1" in sql


def test_a_missing_watchlist_is_null_not_an_empty_list() -> None:
    conn = FakeConn([(IS_TABLE, Reply(one=(False,)))])
    with conn.cursor() as cur:
        out = data_probe._watchlist(cur)
    assert out == {"label": "optionable_stocks", "symbols": None, "count": None, "detail": "missing"}


def test_a_missing_source_is_reported_not_dropped() -> None:
    # neither trade nor strategy_instance is a table
    out = data_probe.read_data_probe(_scripted(missing=2))
    assert out["activity"][0] == {"source": "trades", "last_ts": None, "detail": "missing"}
    assert len(out["activity"]) == 3


def test_a_database_before_r3_answers_with_strategy_instance() -> None:
    """Not renamed yet (or rolled back): ``trade`` is no table, ``strategy_instance`` is."""
    conn = _scripted(missing=1)
    out = data_probe.read_data_probe(conn)
    assert out["activity"][0] == {"source": "trades", "last_ts": "2031-03-04T14:30:00Z"}
    looked_up = [params[0] for text, params in conn.executed if text.startswith(IS_TABLE)]
    assert looked_up[:2] == ["trade", "strategy_instance"]
    assert any("FROM strategy_instance" in text for text, _ in conn.executed)


def test_the_trade_table_is_named_trade_first() -> None:
    assert data_probe.TRADE_TABLES == ("trade", "strategy_instance")
    assert data_probe.CLONE_GROUPS[0][1] == (data_probe.TRADE_TABLES,)
    assert data_probe.SAMPLE == ("trades", data_probe.TRADE_TABLES)


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
    assert trades[0] == "trade"
    for child in ("trade_execution", "strategy_plan", "trade_review", "account_execution_instance_allocation"):
        assert child in trades
    assert "strategy_instance" not in trades  # a compatibility view is never cloned
    # The opportunity group holds the trades group: a trade references its opportunity.
    assert set(trades) <= set(groups["rules"]["tables"])
    assert groups["watchlist"]["tables"][0] == "watchlist"
    assert isinstance(out["sample"]["rows"], int)
    assert {a["source"] for a in out["activity"]} == {"trades", "opportunities", "watchlist"}


@pytest.mark.db
def test_the_watchlist_is_the_optionable_stocks_trimmed_and_distinct(pg_conn) -> None:
    rows = [
        # (contract_key, symbol, sec_type, optionable) -- invented symbols
        ("LANE-R:QZAA:1", "qzaa ", "STK", True),
        ("LANE-R:QZAA:2", "QZAA", "STK", True),
        ("LANE-R:QZBB", "QZBB", "STK", False),
        ("LANE-R:QZCC", "QZCC", "OPT", True),
        ("LANE-R:QZDD", "   ", "STK", True),
        ("LANE-R:QZEE", None, "STK", True),
        ("LANE-R:QZFF", "QZFF", "STK", True),
    ]
    with pg_conn.cursor() as cur:
        for key, symbol, sec_type, optionable in rows:
            cur.execute(
                "INSERT INTO watchlist (contract_key, symbol, sec_type, optionable) VALUES (%s, %s, %s, %s)",
                (key, symbol, sec_type, optionable),
            )
    # the fixture rolls the inserts back
    out = data_probe.read_data_probe(pg_conn)["watchlist"]
    symbols = out["symbols"]
    assert out["label"] == "optionable_stocks"
    assert out["count"] == len(symbols)
    assert symbols == sorted(set(symbols))
    assert [s for s in symbols if s.startswith("QZ")] == ["QZAA", "QZFF"]
