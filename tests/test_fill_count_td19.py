"""TD-19: 'trade' is the strategy instance. Performance counts fills, and says so.

Core 0.38.0 adds ``fill_count`` beside every fill-counting ``trade_count`` (summary,
the realized_by_* breakdowns, the calendar rows), ``pair_count`` on the option
calendar rows (they count closed option pairs, not fills) and ``total_trades`` beside
``total_instances`` on the win rate. ``win_rate`` now divides wins by the fills that
realized a gain or a loss: an opening fill realizes nothing and is not a loss.
The old keys stay one version. Uses the invented book of test_signed_qty.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable

import pytest

from bifrost_core.monitor.reader.strategy_win_rate import _aggregate_win_rate_metrics
from bifrost_core.portfolio.reader import executions as executions_reader
from test_signed_qty import NEW_BOOK, _perf

_FILL_LISTS = (
    "realized_by_account",
    "realized_by_sec_type",
    "realized_by_account_and_sec_type",
    "realized_by_strategy_opportunity",
    "realized_by_strategy_instance",
    "calendar",
)


def _rows(perf: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    yield perf["summary"]
    for key in _FILL_LISTS:
        yield from perf[key]
    yield from (r for r in perf["calendar_by_sec_type"] if r["sec_type"] != "OPT")


@pytest.mark.parametrize("kw", [{}, {"strategy_instance_id": 11}, {"source_scope": "on_the_fly"}])
def test_every_fill_count_is_named(monkeypatch: pytest.MonkeyPatch, kw: Dict[str, Any]) -> None:
    perf = _perf(monkeypatch, NEW_BOOK, **kw)
    rows = list(_rows(perf))
    assert len(rows) > 3
    for row in rows:
        assert row["fill_count"] == row["trade_count"]


def test_option_calendar_rows_count_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    perf = _perf(monkeypatch, NEW_BOOK)
    for row in (r for r in perf["calendar_by_sec_type"] if r["sec_type"] == "OPT"):
        assert row["pair_count"] == row["trade_count"] and "fill_count" not in row


def test_win_rate_is_over_closing_fills(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _perf(monkeypatch, NEW_BOOK)["summary"]
    closing = s["win_count"] + s["loss_count"]
    assert 0 < closing < s["fill_count"]  # the book has opening fills: the two rules differ
    assert s["win_rate"] == round(s["win_count"] / closing, 4)
    assert s["win_rate"] != round(s["win_count"] / s["fill_count"], 4)


def test_instance_summary_counts_fills_and_closing_win_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executions_reader, "get_executions", lambda conn, **_: copy.deepcopy(NEW_BOOK))
    s = executions_reader.get_performance_instance_summary_only(object(), 11)["summary"]
    assert s["fill_count"] == s["trade_count"] > 0
    closing = s["win_count"] + s["loss_count"]
    assert s["win_rate"] == (round(s["win_count"] / closing, 4) if closing else None)


def test_win_rate_names_trades() -> None:
    rows = [{"net_pnl": 10.0, "underlying_cost": 1.0}, {"net_pnl": -5.0, "underlying_cost": 1.0}]
    r = _aggregate_win_rate_metrics("Any", rows)
    assert r["total_trades"] == r["total_instances"] == 2
