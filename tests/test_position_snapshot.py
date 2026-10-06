"""W4 daily book snapshot: the pure parts (splits, vendor ticker, expiry) and enrich's choices."""

from __future__ import annotations

from datetime import date

from bifrost_core.portfolio.snapshot import daily


def _attr(**kw):
    base = {
        "account_id": "U0000001",
        "contract_key": "ZZZ|OPT|20261120|50.0|P",
        "symbol": "ZZZ",
        "sec_type": "OPT",
        "expiry": "20261120",
        "strike": 50.0,
        "option_right": "P",
        "position_qty": -3.0,
        "avg_cost": 120.0,
        "price_last": None,
        "price_mid": 1.25,
        "mark_source": "quote_live",  # the reader labels a fresh live quote (TD-140)
        "trade_id": None,
        "open_qty_est": -3.0,
    }
    base.update(kw)
    return base


def test_vendor_ticker_matches_the_polygon_layout():
    assert daily.vendor_option_ticker("aapl", "20261017", 150, "C") == "O:AAPL261017C00150000"
    assert daily.vendor_option_ticker("ZZZ", "2026-11-20", 82.5, "put") == "O:ZZZ261120P00082500"
    assert daily.vendor_option_ticker("ZZZ", "", 82.5, "P") is None
    assert daily.vendor_option_ticker("ZZZ", "20261120", None, "P") is None


def test_parse_expiry():
    assert daily.parse_expiry("20261120") == date(2026, 11, 20)
    assert daily.parse_expiry("2026-11-20") == date(2026, 11, 20)
    assert daily.parse_expiry("") is None
    assert daily.parse_expiry("20261340") is None


def test_unattributed_position_is_one_null_row():
    rows = daily.split_rows([_attr()])
    assert len(rows) == 1
    r = rows[0]
    assert r["trade_id"] is None and r["trade_qty"] == -3.0 and r["position_qty"] == -3.0
    assert r["expiry"] == date(2026, 11, 20)
    assert (r["mark"], r["mark_source"]) == (1.25, daily.MARK_QUOTE_LIVE)


def test_split_rows_add_up_to_the_position():
    rows = daily.split_rows(
        [
            _attr(trade_id=7, open_qty_est=-2.0, price_last=1.3),
            _attr(trade_id=9, open_qty_est=-0.5, price_last=1.3),
        ]
    )
    by_trade = {r["trade_id"]: r["trade_qty"] for r in rows}
    assert by_trade == {7: -2.0, 9: -0.5, None: -0.5}
    assert sum(by_trade.values()) == -3.0
    assert {r["mark"] for r in rows} == {1.3}


def test_exact_split_has_no_remainder_row_and_zero_trades_drop():
    rows = daily.split_rows(
        [
            _attr(trade_id=7, open_qty_est=-3.0),
            _attr(trade_id=8, open_qty_est=0.0),  # a closed trade on the same contract
        ]
    )
    assert [(r["trade_id"], r["trade_qty"]) for r in rows] == [(7, -3.0)]


def test_no_live_quote_leaves_the_mark_empty():
    rows = daily.split_rows([_attr(price_mid=None, price_last=0)])
    assert (rows[0]["mark"], rows[0]["mark_source"]) == (None, None)


def test_close_on_requires_that_sessions_bar():
    d = date(2026, 10, 5)
    # The plugin's benchmark route: epoch seconds of the bar date (UTC midnight).
    assert daily._close_on({"bar_time": 1791158400.0, "close": 10.5}, d) == 10.5
    assert daily._close_on({"bar_time": 1790899200.0, "close": 10.0}, d) is None  # 10-02
    assert daily._close_on({"bar_time": 1791158400.0, "close": 0}, d) is None  # no close
    assert daily._close_on({"bar_time": 0, "close": 0}, d) is None
    assert daily._close_on({"bar_time": "2026-10-05", "close": 10.5}, d) == 10.5
    assert daily._close_on(None, d) is None


class _Cur:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if sql.lstrip().startswith("SELECT"):
            self._rows = self.store["select"]
        else:
            self.store["updates"].append(params)
            self.rowcount = 1

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows):
        self.store = {"select": rows, "updates": []}

    def cursor(self, **kw):
        return _Cur(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass


def test_enrich_fills_greeks_mark_and_closes():
    d = date(2026, 10, 5)
    conn = _Conn(
        [
            {"position_snapshot_daily_id": 1, "symbol": "ZZZ", "sec_type": "OPT", "expiry": date(2026, 11, 20),
             "strike": 50.0, "option_right": "P", "mark": None, "underlying_close": None, "delta": None, "iv": None},
            {"position_snapshot_daily_id": 2, "symbol": "ZZZ", "sec_type": "STK", "expiry": None,
             "strike": None, "option_right": None, "mark": None, "underlying_close": None, "delta": None, "iv": None},
            {"position_snapshot_daily_id": 3, "symbol": "ZZZ", "sec_type": "OPT", "expiry": date(2026, 11, 20),
             "strike": 45.0, "option_right": "P", "mark": 0.4, "underlying_close": None, "delta": None, "iv": None},
        ]
    )
    chain_calls = []

    def option_rows(sym, exp, as_of):
        chain_calls.append((sym, exp, as_of))
        return [{"option_ticker": "O:ZZZ261120P00050000", "delta": -0.31, "gamma": 0.02, "vega": 0.1,
                 "theta": -0.03, "iv": 0.42, "day_close": 1.2, "snapshot_ts": "2026-10-05T20:00:00Z"}]

    out = daily.enrich(conn, d, option_rows=option_rows,
                       closes=lambda syms, as_of: {"ZZZ": {"bar_time": 1791158400.0, "close": 52.0}})
    assert chain_calls == [("ZZZ", date(2026, 11, 20), d)]  # one chain read per (symbol, expiry)
    assert out == {"rows": 3, "updated": 3, "greeks_missing": 1}
    u = {p["id"]: p for p in conn.store["updates"]}
    assert (u[1]["delta"], u[1]["iv"], u[1]["mark"], u[1]["underlying_close"]) == (-0.31, 0.42, 1.2, 52.0)
    assert (u[2]["mark"], u[2]["underlying_close"], u[2]["delta"]) == (52.0, 52.0, None)
    assert (u[3]["delta"], u[3]["underlying_close"]) == (None, 52.0)  # no vendor row for the 45 put


def test_capture_refuses_an_empty_attribution_over_open_positions(monkeypatch):
    import pytest

    monkeypatch.setattr(daily, "_positions_meta", lambda conn: {("U0000001", "ZZZ|STK|||"): None})
    with pytest.raises(daily.SnapshotError):
        daily.capture(_Conn([]), date(2026, 10, 5), attribution=lambda conn: [])
