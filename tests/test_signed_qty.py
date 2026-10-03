"""TD-30: one signed-quantity rule, and the money that must not move with it.

The rule (``portfolio.signed_qty``): SELL / SLD / S -> -|q|, anything else -> +|q|,
NULL -> NULL, whatever the source. Before core 0.35.0 the readers had five variants;
they are kept below as oracles so the tests can say which outputs change and which
do not. The "stored" classes are the ones measured on Golden Source (2026-10-02):
Flex and journal store a sell negative, TWS stores every fill positive.

Money: Performance, Instance net P&L, the win-rate risk and the option-stock link
slippage are computed here twice -- on the quantities the old rule produced and on
the new ones -- and must come out identical. The SQL side (the parity of the SQL
expression with the Python function, the attribution sum, the readers' columns) is
in ``test_signed_qty_db.py``. Accounts, ids and prices are made up.
"""

from __future__ import annotations

import copy
import itertools
import math
from typing import Any, Dict, List, Optional

import pytest

from bifrost_core.monitor.reader import strategy_win_rate
from bifrost_core.portfolio.reader import accounts as accounts_reader
from bifrost_core.portfolio.reader import executions as executions_reader
from bifrost_core.portfolio.reader import instance_exec_net_pnl as net_pnl
from bifrost_core.portfolio.reader import option_stock_link as link_reader
from bifrost_core.portfolio.signed_qty import SELL_SIDES, is_sell_side, signed_qty, signed_qty_sql
from write_fakes import FakeConn, Reply

ACCOUNT = "U0000001"
SOURCES = ("flex_trades", "journal_closed", "tws_client", "tws_event", None, "")
SIDES = ("BUY", "BOT", "B", "SELL", "SLD", "S", " sell ", "sld", "", None, "XYZ")
QUANTITIES = (3.0, -3.0, 0.0, None)


# --- the five pre-0.35.0 variants (oracles) --------------------------------------------


def _sell(side: Any) -> bool:
    return str(side or "").strip().upper() in SELL_SIDES


def legacy_a(source: Any, side: Any, q: Optional[float]) -> Optional[float]:
    """executions._QTY_NORM(_E), option_stock_link._QTY_NORM_E (SQL)."""
    if q is None:
        return None
    if str(source or "").strip().lower() == "tws_client":
        return q
    return -q if _sell(side) else q


def legacy_b(source: Any, side: Any, q: Optional[float]) -> float:
    """option_stock_link._normalized_signed_qty, accounts._normalized_signed_qty_from_raw."""
    if q is None:
        return 0.0
    if str(source or "").strip().lower() == "tws_client":
        return q
    return -abs(q) if _sell(side) else q


def legacy_c(source: Any, side: Any, q: Optional[float]) -> Optional[float]:
    """executions._SIGNED_QTY_FINAL_ROW_E (attribution, final book)."""
    if q is None:
        return None
    if str(source or "").strip().lower() == "tws_client":
        return q
    return -abs(q) if _sell(side) else abs(q)


def legacy_d(side: Any, q: Optional[float]) -> Optional[float]:
    """executions._SIGNED_QTY_TWS_RAW_ROW_E (attribution, raw TWS)."""
    if q is None:
        return None
    return -abs(q) if _sell(side) else abs(q)


# (source, side, stored quantity) as Golden Source holds them today.
FINAL_BOOK_STORED = [
    ("flex_trades", "SELL", -5.0),
    ("flex_trades", "BUY", 5.0),
    ("journal_closed", "SELL", -2.0),
    ("journal_closed", "BUY", 2.0),
]
TWS_STORED = [
    ("tws_client", "SLD", 4.0),
    ("tws_client", "BOT", 4.0),
    ("tws_client", "SELL", 1.0),
    ("tws_client", "BUY", 1.0),
]


# --- the rule ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source,side,quantity", list(itertools.product(SOURCES, SIDES, QUANTITIES))
)
def test_rule_is_side_only(source: Any, side: Any, quantity: Optional[float]) -> None:
    got = signed_qty(source, side, quantity)
    if quantity is None:
        assert got is None
        return
    expected = -abs(quantity) if is_sell_side(side) else abs(quantity)
    assert got == expected
    # the source never changes the answer
    assert all(signed_qty(s, side, quantity) == got for s in SOURCES)


def test_rule_edges() -> None:
    assert signed_qty("flex_trades", "SELL", "7") == -7.0
    assert signed_qty("flex_trades", "SELL", "x") is None
    assert signed_qty("flex_trades", "SELL", True) is None
    assert math.isnan(signed_qty("flex_trades", "BUY", float("nan")))
    assert is_sell_side(" s ") and not is_sell_side("SHORT") and not is_sell_side(None)


def test_sql_expression_reads_side_and_quantity_only() -> None:
    sql = signed_qty_sql("e")
    assert sql == (
        "CASE WHEN upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S') "
        "THEN -abs(e.quantity) ELSE abs(e.quantity) END"
    )
    assert "source" not in sql
    assert signed_qty_sql(None) == signed_qty_sql("") == sql.replace("e.", "")


def test_every_reader_site_uses_the_one_expression() -> None:
    one = signed_qty_sql("e")
    assert executions_reader._QTY_NORM_E == f"{one} AS quantity"
    assert executions_reader._QTY_NORM == f"{signed_qty_sql(None)} AS quantity"
    assert executions_reader._SIGNED_QTY_ROW_E == one
    assert link_reader._QTY_NORM_E == one
    # tws_raw keeps the stored value (shows what TWS sent); every other scope is signed
    assert executions_reader._qty_expr_e_for_scope("tws_raw") == "e.quantity AS quantity"
    for scope in (None, "all", "performance_book", "on_the_fly"):
        assert executions_reader._qty_expr_e_for_scope(scope) == executions_reader._QTY_NORM_E
    for module in (executions_reader, link_reader):
        assert not hasattr(module, "_SIGNED_QTY_FINAL_ROW_E")
        assert not hasattr(module, "_SIGNED_QTY_TWS_RAW_ROW_E")


# --- what changes and what does not, on today's stored data ---------------------------


@pytest.mark.parametrize("source,side,stored", FINAL_BOOK_STORED)
def test_attribution_final_book_is_unchanged(source: str, side: str, stored: float) -> None:
    assert signed_qty(source, side, stored) == legacy_c(source, side, stored)


@pytest.mark.parametrize("source,side,stored", TWS_STORED)
def test_attribution_raw_tws_is_unchanged(source: str, side: str, stored: float) -> None:
    assert signed_qty(source, side, stored) == legacy_d(side, stored)


@pytest.mark.parametrize("source,side,stored", FINAL_BOOK_STORED + TWS_STORED)
def test_executions_quantity_flips_for_sells_only(source: str, side: str, stored: float) -> None:
    """``/executions`` ``quantity`` (scopes all / performance_book / on_the_fly): every sell
    read back positive before and reads back negative now; buys are unchanged."""
    before = legacy_a(source, side, stored)
    after = signed_qty(source, side, stored)
    if _sell(side):
        assert before == abs(stored) and after == -abs(stored)
    else:
        assert before == after == abs(stored)


@pytest.mark.parametrize("source,side,stored", FINAL_BOOK_STORED + TWS_STORED)
def test_allocation_sum_expected_is_what_the_form_sends(source: str, side: str, stored: float) -> None:
    """ExecutionFormModal sends -|q| for a sell and +|q| for a buy. Flex / journal already
    matched; TWS sells (stored positive) expected +|q| and refused the form's split."""
    form_sum = -abs(stored) if _sell(side) else abs(stored)
    assert accounts_reader._normalized_signed_qty_from_raw(source, side, stored) == form_sum
    if source == "tws_client" and _sell(side):
        assert legacy_b(source, side, stored) != form_sum
    else:
        assert legacy_b(source, side, stored) == form_sum


class _AllocCursor:
    """Answers the raw-row read and the instance-account check; records inserts."""

    def __init__(self, raw_row: Any) -> None:
        self.raw_row = raw_row
        self.executed: List[Any] = []
        self._last = ""

    def execute(self, sql: str, params: Any = None) -> None:
        self._last = sql
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if "FROM strategy_instance" in self._last:
            return (ACCOUNT,)
        return self.raw_row


@pytest.mark.parametrize(
    "stored,side,source,splits,ok",
    [
        (4.0, "SLD", "tws_client", [-1.0, -3.0], True),  # refused before 0.35.0
        (4.0, "SLD", "tws_client", [1.0, 3.0], False),  # accepted before 0.35.0
        (-5.0, "SELL", "flex_trades", [-2.0, -3.0], True),
        (5.0, "BUY", "flex_trades", [2.0, 3.0], True),
        (-2.0, "SELL", "journal_closed", [-2.0], True),
        (None, "BUY", None, [1.0], False),  # the quantity-less rows: nothing to split
    ],
)
def test_allocation_sum_check(stored: Any, side: str, source: Any, splits: List[float], ok: bool) -> None:
    cur = _AllocCursor((ACCOUNT, stored, side, source, "td.e1"))
    body = [{"strategy_instance_id": 10 + i, "allocated_quantity": q} for i, q in enumerate(splits)]
    got = accounts_reader._apply_instance_allocations_on_cursor(
        cur, -7, "raw_broker.executions_raw_tws", "executions_raw_tws_id", 7, body
    )
    assert got is ok
    inserts = [p for sql, p in cur.executed if "INSERT INTO" in sql]
    assert len(inserts) == (len(splits) if ok else 0)


# --- money: computed on the old quantities and on the new ones -------------------------


def _book(sign_rule: Any) -> List[Dict[str, Any]]:
    """An invented performance book as get_executions returns it, quantity per ``sign_rule``."""
    rows = [
        # (id, source, sec, side, stored qty, price, commission, realized, instance)
        (1, "flex_trades", "OPT", "SELL", -2.0, 1.50, 1.30, 0.0, 11),
        (2, "flex_trades", "OPT", "BUY", 2.0, 0.40, 1.30, 210.0, 11),
        (3, "flex_trades", "STK", "SELL", -100.0, 51.0, 1.00, 25.0, 11),
        (4, "journal_closed", "OPT", "SELL", -1.0, 2.00, 0.0, -40.0, None),
        (5, "flex_trades", "OPT", "SLD", -4.0, 1.00, 2.00, 0.0, None),
        (6, "flex_trades", "STK", "BUY", 100.0, 49.0, 1.00, 0.0, 12),
    ]
    out = []
    for i, (eid, src, sec, side, q, price, comm, rp, si) in enumerate(rows):
        row = {
            "account_executions_id": eid, "account_id": ACCOUNT, "source": src, "sec_type": sec,
            "side": side, "quantity": sign_rule(src, side, q), "price": price, "commission": comm,
            "realized_pnl": rp, "strategy_instance_id": si, "time": 1_790_000_000.0 + 3600 * i,
            "contract_key": f"ZZZ|{sec}|20261120|50|C" if sec == "OPT" else "ZZZ|STK|||",
            "strike": 50 if sec == "OPT" else None, "option_right": "C" if sec == "OPT" else None,
            "instance_allocations": [],
        }
        if eid == 5:  # split across two instances, signed the way the form sends it
            row["instance_allocations"] = [
                {"strategy_instance_id": 11, "allocated_quantity": -1.0, "strategy_opportunity_id": 3},
                {"strategy_instance_id": 12, "allocated_quantity": -3.0, "strategy_opportunity_id": 3},
            ]
        out.append(row)
    return out


OLD_BOOK = _book(legacy_a)
NEW_BOOK = _book(signed_qty)


def test_the_two_books_differ_only_in_sell_quantities() -> None:
    for old, new in zip(OLD_BOOK, NEW_BOOK):
        diff = {k for k in old if old[k] != new[k]}
        assert diff == ({"quantity"} if _sell(old["side"]) else set())
        assert abs(old["quantity"]) == abs(new["quantity"])


def _perf(monkeypatch: pytest.MonkeyPatch, book: List[Dict[str, Any]], **kw: Any) -> Dict[str, Any]:
    monkeypatch.setattr(executions_reader, "get_executions", lambda conn, **_: copy.deepcopy(book))
    monkeypatch.setattr(executions_reader, "_get_current_equity", lambda conn: 100_000.0)
    monkeypatch.setattr(executions_reader, "get_net_cash_flow", lambda conn, **_: 0.0)
    monkeypatch.setattr(executions_reader, "get_transactions", lambda conn, **_: [])
    monkeypatch.setattr(accounts_reader, "get_accounts_from_tables", lambda conn: [])
    return executions_reader.get_performance_stats(object(), since_ts=1.0, until_ts=2e9, **kw)


@pytest.mark.parametrize("kw", [{}, {"strategy_instance_id": 11}, {"source_scope": "on_the_fly"}])
def test_performance_is_unchanged(monkeypatch: pytest.MonkeyPatch, kw: Dict[str, Any]) -> None:
    old = _perf(monkeypatch, OLD_BOOK, **kw)
    new = _perf(monkeypatch, NEW_BOOK, **kw)
    assert new == old
    assert old["summary"]["fill_count"] > 0


def test_performance_instance_summary_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    out = []
    for book in (OLD_BOOK, NEW_BOOK):
        monkeypatch.setattr(executions_reader, "get_executions", lambda conn, _b=book, **_: copy.deepcopy(_b))
        out.append(executions_reader.get_performance_instance_summary_only(object(), 11))
    assert out[0] == out[1]


def _instance_net(monkeypatch: pytest.MonkeyPatch, book: List[Dict[str, Any]], links: Dict[str, Any]) -> float:
    monkeypatch.setattr(net_pnl, "get_executions", lambda conn, **_: copy.deepcopy(book))
    monkeypatch.setattr(net_pnl, "get_option_stock_links_bulk", lambda conn, batches: links)
    return net_pnl.compute_instance_exec_derived_net_pnl(object(), 11)


def test_instance_exec_net_pnl_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    links = {"by_option_id": {"1": {"slippage_total": -6.0}, "5": {"slippage_total": 4.0}}}
    old = _instance_net(monkeypatch, OLD_BOOK, links)
    new = _instance_net(monkeypatch, NEW_BOOK, links)
    assert new == old != 0.0


def test_win_rate_max_risk_is_unchanged() -> None:
    def put_spread(rule: Any) -> List[Dict[str, Any]]:
        legs = [("SELL", -2.0, 50, 2.10), ("BUY", 2.0, 45, 0.60)]  # stored as Flex stores them
        return [
            {"sec_type": "OPT", "side": side, "quantity": rule("flex_trades", side, q), "price": px,
             "strike": k, "option_right": "P", "contract_key": f"ZZZ|OPT|20261120|{k}|P"}
            for side, q, k, px in legs
        ]

    old = strategy_win_rate._instance_max_risk_from_executions(put_spread(legacy_a), 0.0)
    new = strategy_win_rate._instance_max_risk_from_executions(put_spread(signed_qty), 0.0)
    assert new == old > 0.0


def _link_rows(sign_rule: Any) -> List[Dict[str, Any]]:
    """Link rows as the SQL returns them: stock_quantity per ``sign_rule`` on stored values."""
    stored = [("flex_trades", "SELL", -100.0, 52.0, 50.0), ("flex_trades", "BUY", 100.0, 48.5, 50.0),
              ("journal_closed", "SELL", -30.0, 49.0, 50.0), ("flex_trades", "BUY", None, 50.0, 50.0)]
    return [
        {
            "link_id": i + 1, "option_account_executions_id": 1, "stock_account_executions_id": 30 + i,
            "stock_side": side, "stock_quantity": sign_rule(src, side, q), "stock_price": p,
            "stock_close_price": cp,
        }
        for i, (src, side, q, p, cp) in enumerate(stored)
    ]


@pytest.mark.parametrize("reader", ["one", "bulk"])
def test_option_stock_link_slippage_is_unchanged(reader: str) -> None:
    out = []
    for rule in (legacy_a, signed_qty):
        conn = FakeConn([("account_execution_option_stock_link", Reply(all=_link_rows(rule)))])
        if reader == "one":
            res = link_reader.get_option_stock_links(conn, ACCOUNT, 1)
        else:
            res = link_reader.get_option_stock_links_bulk(conn, [(ACCOUNT, [1])])["by_option_id"]["1"]
        out.append(res)
    old, new = out
    assert new["slippage_total"] == old["slippage_total"] == pytest.approx(200.0 - 150.0 - 30.0)
    assert [x["slippage_vs_close"] for x in new["links"]] == [x["slippage_vs_close"] for x in old["links"]]
    # the quantity column itself is now signed
    assert [x["stock_quantity"] for x in new["links"]] == [-100.0, 100.0, -30.0, None]
    assert [x["stock_quantity"] for x in old["links"]] == [100.0, 100.0, 30.0, None]


def test_stock_link_candidates_slippage_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    opt = {"account_id": ACCOUNT, "sec_type": "OPT", "underlying_symbol": "ZZZ", "trade_date": "2026-09-18"}
    monkeypatch.setattr(link_reader, "fetch_execution_final_row", lambda conn, acc, oid: dict(opt))
    out = []
    for rule in (legacy_a, signed_qty):
        rows = [
            {"account_executions_id": 30, "side": "SELL", "quantity": rule("flex_trades", "SELL", -100.0),
             "price": 52.0, "close_price": 50.0, "time": 1.0},
        ]
        conn = FakeConn([("FROM brokerage.executions_final e", Reply(all=rows))])
        out.append(link_reader.get_stock_link_candidates(conn, ACCOUNT, 1)["executions"])
    assert out[0][0]["slippage_vs_close"] == out[1][0]["slippage_vs_close"] == 200.0
    assert (out[0][0]["quantity"], out[1][0]["quantity"]) == (100.0, -100.0)
