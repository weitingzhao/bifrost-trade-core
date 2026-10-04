"""The execution views name every column once (TD-13).

Golden Source's views carry the IB Flex TradeID as ``trade_id`` next to ``related_trade_id``.
The Rev .111 rename plan once mapped the strategy attribution to ``trade_id`` as well:
the first DDL would have failed on a duplicate column, and a hand fix would have left
one name for two ids. Naming R3 (core 0.45.0) renamed the env views' attribution to
``trade_id`` and aliased the IB columns (``ib_trade_id`` / ``ib_related_trade_id``) in the
same change, keeping ``strategy_instance_id`` (= ``trade_id``) one version; naming R4
(core 0.47.0) dropped it and the ``instance_allocations`` view.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import List

import pytest

from bifrost_core.persistence.postgres.brokerage_ddl import _EXEC_CANONICAL_COLS, _create_brokerage_views


class _Recorder:
    def __init__(self) -> None:
        self.sql: List[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.sql.append(sql)


def _output_columns(view_sql: str) -> List[str]:
    """Column names of a CREATE VIEW's outermost SELECT list (the text up to its FROM)."""
    body = re.split(r"VIEW \S+ AS\s", view_sql, maxsplit=1)[1]
    select = re.search(r"SELECT\s+(.*?)\s+FROM\s", body, re.S)
    assert select, view_sql
    names = []
    for expr in select.group(1).split(","):
        expr = expr.strip()
        alias = re.search(r"\bAS\s+(\w+)$", expr, re.I)
        names.append(alias.group(1) if alias else expr.rsplit(".", 1)[-1])
    return names


def _views(env: bool) -> dict:
    rec = _Recorder()
    _create_brokerage_views(rec, "brokerage", env=env)
    out = {}
    for sql in rec.sql:
        m = re.match(r"\s*CREATE OR REPLACE VIEW brokerage\.(\w+) AS", sql)
        if m and m.group(1) != "trade_fill_splits":
            out[m.group(1)] = _output_columns(sql)
    return out


def test_canonical_columns_are_unique() -> None:
    cols = [c.strip() for c in _EXEC_CANONICAL_COLS.split(",") if c.strip()]
    assert [c for c, n in Counter(cols).items() if n > 1] == []


@pytest.mark.parametrize("env", [False, True], ids=["golden_source", "per_env"])
def test_each_execution_view_names_every_column_once(env: bool) -> None:
    views = _views(env)
    assert set(views) >= {"executions", "executions_final", "executions_fly"}
    for name, cols in views.items():
        assert [c for c, n in Counter(cols).items() if n > 1] == [], name
        # one trade_id: on Golden Source the IB TradeID, in an env the attribution (R3)
        assert cols.count("trade_id") == 1, name
        assert cols[0] == "account_executions_id", name
        if env:
            assert {"ib_trade_id", "ib_related_trade_id"} <= set(cols), name
            assert "related_trade_id" not in cols, name
            # naming R4: the one-version alias of the attribution is gone
            assert "strategy_instance_id" not in cols, name
        else:
            assert "ib_trade_id" not in cols and "related_trade_id" in cols, name


def test_split_views_name_their_columns() -> None:
    rec = _Recorder()
    _create_brokerage_views(rec, "brokerage", env=True)
    sql = {
        m.group(1): s
        for s in rec.sql
        if (m := re.match(r"\s*CREATE OR REPLACE VIEW brokerage\.(\w+) AS", s))
    }
    assert _output_columns(sql["trade_fill_splits"]) == [
        "account_id", "account_executions_id", "trade_id", "quantity", "exec_id",
    ]
    # naming R4: R3's compatibility view is not made any more, and is dropped by name first
    assert "instance_allocations" not in sql
    assert rec.sql[0] == "DROP VIEW IF EXISTS brokerage.instance_allocations"
