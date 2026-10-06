"""W4 daily book snapshot: the pure parts (splits, vendor ticker, expiry) and enrich's choices."""

from __future__ import annotations

from datetime import date, datetime, timezone

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

    def close(self):
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


# --------------------------------------------------------------------------- capture (0.51.0)

D = date(2026, 10, 5)
CLOSE = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)  # 16:00 New York (EDT)
AFTER = datetime(2026, 10, 5, 20, 12, tzinfo=timezone.utc)
INTRADAY = datetime(2026, 10, 5, 15, 48, tzinfo=timezone.utc)


class _WCur:
    def __init__(self, log):
        self.log = log
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self.rowcount = 1


class _WConn:
    def __init__(self):
        self.log = []
        self.commits = 0

    def cursor(self, **kw):
        return _WCur(self.log)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def inserts(self, table):
        return [p for sql, p in self.log if f"INSERT INTO {table}" in sql]


def _account(acct, updated_at, **extra):
    return {"account_id": acct, "updated_at": updated_at, "net_liquidation": 1000.0, "total_cash": 100.0,
            "buying_power": 2000.0, "summary_extra": extra or None}


def _book(monkeypatch, accounts, positions, captured=()):
    monkeypatch.setattr(daily, "session_close_at", lambda conn, d: CLOSE)
    monkeypatch.setattr(daily, "_accounts", lambda conn: [dict(a) for a in accounts])
    monkeypatch.setattr(daily, "_captured_accounts", lambda conn, d: set(captured))
    monkeypatch.setattr(daily, "_positions_meta", lambda conn: dict(positions))


def _never_called(conn):
    raise AssertionError("attribution must not be read")


def test_summary_extra_values_parse_strings_and_drop_non_finite():
    assert daily.summary_extra_values({"Cushion": "0.42", "ExcessLiquidity": "1.5E3", "MaintMarginReq": 7}) == {
        "cushion": 0.42, "excess_liquidity": 1500.0, "maint_margin_req": 7.0}
    assert daily.summary_extra_values({"Cushion": "inf", "ExcessLiquidity": "NaN", "MaintMarginReq": ""}) == {
        "cushion": None, "excess_liquidity": None, "maint_margin_req": None}
    assert daily.summary_extra_values(None) == {"cushion": None, "excess_liquidity": None, "maint_margin_req": None}
    assert daily.summary_extra_values("not a dict")["cushion"] is None


def test_capture_refuses_an_empty_attribution_over_open_positions(monkeypatch):
    import pytest

    _book(monkeypatch, [_account("U0000001", AFTER)], {("U0000001", "ZZZ|STK|||"): AFTER})
    conn = _WConn()
    with pytest.raises(daily.SnapshotError):
        daily.capture(conn, D, attribution=lambda conn: [])
    assert conn.inserts("account_nav_daily") == [] and conn.commits == 0


def test_capture_skips_a_stale_account_whole_and_lists_it(monkeypatch):
    _book(
        monkeypatch,
        [_account("U0000001", AFTER, Cushion="0.5", ExcessLiquidity="900", MaintMarginReq="nan"),
         _account("U0000002", INTRADAY), _account("U0000003", None)],
        {("U0000001", "ZZZ|OPT|20261120|50.0|P"): AFTER, ("U0000002", "YYY|STK|||"): INTRADAY},
    )
    conn = _WConn()
    attr = [_attr(), _attr(account_id="U0000002", contract_key="YYY|STK|||", sec_type="STK", symbol="YYY",
                           expiry=None, strike=None, option_right=None, position_qty=10.0, open_qty_est=10.0)]
    out = daily.capture(conn, D, attribution=lambda c: attr)
    navs = conn.inserts("account_nav_daily")
    assert [n["account_id"] for n in navs] == ["U0000001"]
    assert (navs[0]["cushion"], navs[0]["excess_liquidity"], navs[0]["maint_margin_req"]) == (0.5, 900.0, None)
    assert {p["account_id"] for p in conn.inserts("position_snapshot_daily")} == {"U0000001"}
    assert out["nav_rows"] == 1 and out["position_rows"] == 1 and out["position_rows_seen"] == 1
    assert out["stale_accounts"] == [
        {"account_id": "U0000002", "updated_at": INTRADAY.isoformat()},
        {"account_id": "U0000003", "updated_at": None},
    ]
    assert out["session_close"] == CLOSE.isoformat()


def test_a_stale_account_with_positions_does_not_trip_the_empty_attribution_guard(monkeypatch):
    # The fresh account has no positions; the stale one has, and the attribution answers only for it.
    _book(monkeypatch, [_account("U0000001", AFTER), _account("U0000002", INTRADAY)],
          {("U0000002", "YYY|STK|||"): INTRADAY})
    conn = _WConn()
    out = daily.capture(conn, D, attribution=lambda c: [])
    assert out["nav_rows"] == 1 and out["position_rows"] == 0
    assert [s["account_id"] for s in out["stale_accounts"]] == ["U0000002"]


def test_the_guard_counts_only_the_accounts_being_written(monkeypatch):
    import pytest

    # Attribution came back for the stale account only: the fresh account's book would be lost.
    _book(monkeypatch, [_account("U0000001", AFTER), _account("U0000002", INTRADAY)],
          {("U0000001", "ZZZ|OPT|20261120|50.0|P"): AFTER, ("U0000002", "YYY|STK|||"): INTRADAY})
    with pytest.raises(daily.SnapshotError):
        daily.capture(_WConn(), D, attribution=lambda c: [_attr(account_id="U0000002", contract_key="YYY|STK|||")])


def test_all_stale_reads_no_attribution_and_writes_nothing(monkeypatch):
    _book(monkeypatch, [_account("U0000002", INTRADAY)], {("U0000002", "YYY|STK|||"): INTRADAY})
    conn = _WConn()
    out = daily.capture(conn, D, attribution=_never_called)
    assert conn.log == [] and out["nav_rows"] == 0 and len(out["stale_accounts"]) == 1


def test_evening_rerun_takes_only_the_account_that_came_back(monkeypatch):
    # 16:20 wrote U0000001; U0000002 was stale then and has synced since the close.
    _book(monkeypatch, [_account("U0000001", AFTER), _account("U0000002", AFTER)],
          {("U0000001", "ZZZ|OPT|20261120|50.0|P"): AFTER, ("U0000002", "YYY|STK|||"): AFTER},
          captured={"U0000001"})
    conn = _WConn()
    attr = [_attr(), _attr(account_id="U0000002", contract_key="YYY|STK|||", sec_type="STK", symbol="YYY",
                           expiry=None, strike=None, option_right=None, position_qty=10.0, open_qty_est=10.0)]
    out = daily.capture(conn, D, attribution=lambda c: attr)
    assert [n["account_id"] for n in conn.inserts("account_nav_daily")] == ["U0000002"]
    assert [p["account_id"] for p in conn.inserts("position_snapshot_daily")] == ["U0000002"]
    assert out["already_captured"] == ["U0000001"] and out["stale_accounts"] == []


def test_evening_rerun_with_everything_written_is_a_no_op(monkeypatch):
    _book(monkeypatch, [_account("U0000001", AFTER)], {("U0000001", "ZZZ|OPT|20261120|50.0|P"): AFTER},
          captured={"U0000001"})
    conn = _WConn()
    out = daily.capture(conn, D, attribution=_never_called)
    assert conn.log == [] and out["nav_rows"] == 0 and out["already_captured"] == ["U0000001"]


def test_positions_without_an_account_row_count_as_stale(monkeypatch):
    _book(monkeypatch, [], {("U0000009", "YYY|STK|||"): AFTER})
    out = daily.capture(_WConn(), D, attribution=_never_called)
    assert out["stale_accounts"] == [{"account_id": "U0000009", "updated_at": None}]


def _run_main(monkeypatch, capture_fn):
    import bifrost_core.monitor.reader.write_support as ws
    import bifrost_core.persistence.postgres.connection as connection
    from bifrost_core.portfolio.snapshot import __main__ as cli

    monkeypatch.setattr(ws, "open_conn", lambda config: _Conn([]))
    monkeypatch.setattr(connection, "get_conn_params", lambda config: {"dbname": "bifrost_test"})
    monkeypatch.setattr(daily, "session_date_ny", lambda conn: D)
    monkeypatch.setattr(daily, "is_closed_session", lambda conn, d: False)
    monkeypatch.setattr(daily, "capture", capture_fn)
    calls = []
    monkeypatch.setattr(daily, "enrich", lambda conn, d: calls.append(d) or {"rows": 0})
    return cli, calls


def test_all_still_enriches_when_capture_fails(monkeypatch, capsys):
    def boom(conn, d):
        raise daily.SnapshotError("attribution read returned none")

    cli, calls = _run_main(monkeypatch, boom)
    assert cli.main(["all"]) == 1
    assert calls == [D]
    assert '"capture_error"' in capsys.readouterr().out


def test_capture_alone_still_fails_without_enrich(monkeypatch):
    def boom(conn, d):
        raise daily.SnapshotError("attribution read returned none")

    cli, calls = _run_main(monkeypatch, boom)
    assert cli.main(["capture"]) == 1
    assert calls == []
