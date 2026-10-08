"""Attribution prices without a live quote (TD-140, core 0.51.0).

Nothing writes ``contract_quote_live`` (TD-240), so the reader no longer joins it (TD-260). A row
takes the newest vendor session close -- the snapshot's vendor-EOD mark, or a newer plugin daily
close for a stock -- labelled ``vendor_eod`` with its date. The nightly snapshot must not take
that fallback as its own mark: capture reads without it, and ``split_rows`` refuses a non-live
mark whoever the caller is.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from bifrost_core.portfolio.quote_freshness import MARK_EOD_SOURCES, MARK_QUOTE_LIVE, MARK_VENDOR_EOD
from bifrost_core.portfolio.reader import executions
from bifrost_core.portfolio.reader.executions import _build_attribution_rows, label_marks
from bifrost_core.portfolio.snapshot import daily

OPT = "ZZQ|OPT|20311121|40.0|P"
STK = "ZZQ|STK|||"


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.queries.append((sql, params))
        if self._conn.fail:
            raise RuntimeError("relation does not exist")

    def fetchall(self) -> List[Any]:
        keys = set(self._conn.queries[-1][1][0])
        return [r for r in self._conn.snapshot if r[0] in keys]


class _Conn:
    """Answers the snapshot-mark query with ``snapshot`` rows (already newest-per-contract)."""

    def __init__(self, snapshot: Optional[List[Any]] = None, fail: bool = False) -> None:
        self.snapshot = snapshot or []
        self.fail = fail
        self.queries: List[Any] = []
        self.rolled_back = 0

    def cursor(self, **kw: Any) -> _Cur:
        return _Cur(self)

    def rollback(self) -> None:
        self.rolled_back += 1


def _row(contract_key: str = OPT, sec_type: str = "OPT", **kw: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "account_id": "U0000001",
        "contract_key": contract_key,
        "symbol": "ZZQ",
        "sec_type": sec_type,
        "expiry": "20311121" if sec_type == "OPT" else "",
        "strike": 40.0 if sec_type == "OPT" else None,
        "option_right": "P" if sec_type == "OPT" else "",
        "position_qty": -2.0 if sec_type == "OPT" else 100.0,
        "avg_cost": 150.0 if sec_type == "OPT" else 30.0,  # per contract for options, as IB reports it
        "trade_id": 7,
        "net_qty_contribution": -2.0 if sec_type == "OPT" else 100.0,
        "exec_count": 1,
    }
    base.update(kw)
    return base


def _epoch(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def _no_plugin(symbols: List[str]) -> Dict[str, Any]:
    raise AssertionError(f"plugin read for {symbols}")


# --------------------------------------------------------------------------- the reader


def test_no_live_quote_takes_the_snapshots_vendor_eod_mark():
    conn = _Conn(snapshot=[(OPT, 1.2, date(2031, 10, 6))])
    rows = [_row()]
    label_marks(conn, rows, stock_closes=_no_plugin)
    out = _build_attribution_rows(rows)[0]
    assert out["price_last"] == 1.2
    assert "price_mid" not in out  # a close is not a mid, and nothing serves a live one
    assert (out["mark_source"], out["mark_date"]) == (MARK_VENDOR_EOD, "2031-10-06")
    assert out["unrealized_pnl_est"] == pytest.approx((1.2 - 1.5) * -2 * 100)
    sql, params = conn.queries[0]
    # Every end-of-day source, not vendor_eod alone: enrich's TD-246 substitutes are that session's EOD.
    assert "position_snapshot_daily" in sql and params == ([OPT], list(MARK_EOD_SOURCES))


def test_the_reader_does_not_join_contract_quote_live():
    """TD-260: the table has no writer; a join could only ever serve March rows or nothing."""
    src = inspect.getsource(executions.get_position_instance_attribution)
    assert "CONTRACT_QUOTE_LIVE" not in src and "fresh_quote_sql" not in src


def test_a_price_on_the_sql_row_is_not_taken_as_live():
    rows = [_row(price_last=0.95)]
    label_marks(_Conn(), rows, stock_closes=_no_plugin)
    assert (rows[0]["price_last"], rows[0]["mark_source"]) == (None, None)


def test_a_stock_takes_the_newer_of_snapshot_and_plugin_close():
    snap = [(STK, 31.0, date(2031, 10, 6))]
    newer = {"ZZQ": {"bar_time": _epoch(date(2031, 10, 7)), "close": 32.5}}
    rows = [_row(STK, "STK")]
    label_marks(_Conn(snapshot=snap), rows, stock_closes=lambda s: newer)
    assert (rows[0]["price_last"], rows[0]["mark_source"], rows[0]["mark_date"]) == (32.5, MARK_VENDOR_EOD, "2031-10-07")

    older = {"ZZQ": {"bar_time": _epoch(date(2031, 10, 3)), "close": 29.0}}
    rows = [_row(STK, "STK")]
    label_marks(_Conn(snapshot=snap), rows, stock_closes=lambda s: older)
    assert (rows[0]["price_last"], rows[0]["mark_date"]) == (31.0, "2031-10-06")


def test_a_stock_with_no_snapshot_row_uses_the_plugin_close():
    seen: List[List[str]] = []

    def closes(symbols: List[str]) -> Dict[str, Any]:
        seen.append(symbols)
        return {"ZZQ": {"bar_time": "2031-10-07", "close": 32.5}, "ZZR": {"bar_time": 0, "close": 0}}

    rows = [_row(STK, "STK"), _row("ZZR|STK|||", "STK", symbol="ZZR")]
    label_marks(_Conn(), rows, stock_closes=closes)
    assert seen == [["ZZQ", "ZZR"]]  # one bulk read
    assert (rows[0]["price_last"], rows[0]["mark_date"]) == (32.5, "2031-10-07")
    assert (rows[1]["price_last"], rows[1]["mark_source"]) == (None, None)  # a 0 close is none


def test_options_never_read_the_stock_closes():
    rows = [_row()]
    label_marks(_Conn(), rows, stock_closes=_no_plugin)
    assert (rows[0]["price_last"], rows[0]["mark_source"], rows[0]["mark_date"]) == (None, None, None)


def test_unreadable_sources_leave_the_row_unpriced_not_the_read_failed():
    def down(symbols: List[str]) -> Dict[str, Any]:
        raise OSError("plugin down")

    conn = _Conn(fail=True)
    rows = [_row(), _row(STK, "STK")]
    label_marks(conn, rows, stock_closes=down)
    assert [r["mark_source"] for r in rows] == [None, None]
    assert conn.rolled_back == 1
    assert len(_build_attribution_rows(rows)) == 2


def test_fallback_off_reads_neither_source():
    conn = _Conn(snapshot=[(OPT, 1.2, date(2031, 10, 6))])
    rows = [_row(), _row(STK, "STK")]
    label_marks(conn, rows, fallback=False, stock_closes=_no_plugin)
    assert conn.queries == []
    assert [(r["price_last"], r["mark_source"]) for r in rows] == [(None, None), (None, None)]


# --------------------------------------------------------------------------- the snapshot guard


def test_capture_reads_attribution_without_the_fallback(monkeypatch):
    calls: List[Dict[str, Any]] = []

    def fake(conn: Any, *a: Any, **kw: Any) -> List[Dict[str, Any]]:
        calls.append(kw)
        return []

    monkeypatch.setattr(executions, "get_position_instance_attribution", fake)
    assert daily.attribution_live_marks_only(object()) == []
    assert calls == [{"fallback_marks": False}]


def test_split_rows_refuses_a_vendor_eod_mark():
    """Whatever reader capture is handed: yesterday's close must not become today's mark."""
    labelled = [_row(price_last=1.2, mark_source=MARK_VENDOR_EOD, mark_date="2031-10-06", open_qty_est=-2.0)]
    rows = daily.split_rows(labelled)
    assert [(r["mark"], r["mark_source"]) for r in rows] == [(None, None)]

    unlabelled = [_row(price_last=1.2, open_qty_est=-2.0)]  # no mark_source: not known to be live
    assert daily.split_rows(unlabelled)[0]["mark"] is None

    live = [_row(price_last=1.25, mark_source=MARK_QUOTE_LIVE, open_qty_est=-2.0)]
    assert [(r["mark"], r["mark_source"]) for r in daily.split_rows(live)] == [(1.25, MARK_QUOTE_LIVE)]
