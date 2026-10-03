"""The execution views name every column once (TD-13).

The views already carry the IB Flex TradeID as ``trade_id`` next to ``related_trade_id``.
The Rev .111 rename plan once mapped the strategy attribution to ``trade_id`` as well:
the first DDL would have failed on a duplicate column, and a hand fix would have left
one name for two ids. When the attribution is renamed, the IB columns are aliased
(ib_trade_id / ib_related_trade_id) in the same change; this test fails first if not.
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
    body = view_sql.split(" AS ", 1)[1]
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
        if m and m.group(1) != "instance_allocations":
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
        # one trade_id: today the IB TradeID; after the Rev .111 rename, the attribution
        assert cols.count("trade_id") == 1, name
        assert cols[0] == "account_executions_id", name
