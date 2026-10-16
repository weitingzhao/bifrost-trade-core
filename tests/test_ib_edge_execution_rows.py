"""The daemon writes the snapshot's fills under the sink's names, and only complete ones (core 0.60.1).

``refresh_accounts_from_redis_edge`` handed ``last_execution_rows`` (the IB Gateway plugin's fills:
``account`` / ``shares`` / ``ts``, no ``source``) straight to ``write_account_executions``, which
reads ``account_id`` / ``quantity`` / ``time`` / ``source``. On PROD 2026-10-15 two fills were
stored with NULL account, quantity, time and source, and the Ledger page failed to render on the
stock one. The daemon now maps them with ``execution_rows_from_gateway_fills``, as
``POST /executions/fetch`` already did. Accounts, symbols and numbers here are invented.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from bifrost_core.portfolio import ib_edge

STOCK_FILL = {
    "exec_id": "0000a.0001.01.01", "account": "U0000001", "symbol": "zzq", "sec_type": "STK",
    "side": "SLD", "shares": 10.0, "price": 1.5, "commission": 0.1, "realized_pnl": -0.2, "ts": 1792000000.0,
}
OPTION_FILL = {
    "exec_id": "0000b.0002.01.01", "account": "U0000001", "symbol": "ZZQ", "sec_type": "OPT",
    "side": "BOT", "shares": 1.0, "price": 2.5, "commission": 1.0, "realized_pnl": 3.0, "ts": 1792000060.0,
}


class _Recording:
    def __init__(self) -> None:
        self.executions: List[List[Dict[str, Any]]] = []

    def write_open_orders(self, orders: Any) -> None:
        pass

    def write_account_executions(self, rows: Any) -> None:
        self.executions.append(list(rows))


def _refresh(monkeypatch: pytest.MonkeyPatch, fills: List[Dict[str, Any]], writes_off: bool = False) -> _Recording:
    r = MagicMock()
    r.get.return_value = json.dumps({"accounts_snapshot": [], "last_execution_rows": fills})
    monkeypatch.setattr(ib_edge, "_redis_sync_client", lambda cfg: r)
    monkeypatch.setattr(ib_edge, "daemon_broker_writes_off", lambda: writes_off)
    sink = _Recording()
    app = SimpleNamespace(config={}, store=MagicMock(), _host_account_id=None, _status_sink=sink, symbol="")
    asyncio.run(ib_edge.refresh_accounts_from_redis_edge(app))
    return sink


def test_a_plugin_stock_fill_is_written_under_the_sinks_names(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _refresh(monkeypatch, [STOCK_FILL])
    assert len(sink.executions) == 1
    (row,) = sink.executions[0]
    assert row["account_id"] == "U0000001"
    assert row["quantity"] == 10.0
    assert row["time"] == 1792000000.0
    assert row["source"] == "tws_client"
    assert row["contract_key"] == "ZZQ|STK|||"
    assert row["exec_id"] == "0000a.0001.01.01"
    assert "account" not in row and "shares" not in row and "ts" not in row


def test_an_option_fill_without_its_contract_is_not_written(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=ib_edge.__name__):
        sink = _refresh(monkeypatch, [OPTION_FILL])
    assert sink.executions == []
    assert "'0000b.0002.01.01' missing expiry,strike,option_right" in caplog.text


@pytest.mark.parametrize("dropped", ["account", "shares", "side", "exec_id"])
def test_a_fill_without_a_required_field_is_not_written(monkeypatch: pytest.MonkeyPatch, dropped: str) -> None:
    fill = {k: v for k, v in STOCK_FILL.items() if k != dropped}
    assert _refresh(monkeypatch, [fill]).executions == []


def test_only_the_complete_fills_of_a_mixed_snapshot_are_written(monkeypatch: pytest.MonkeyPatch) -> None:
    sink = _refresh(monkeypatch, [OPTION_FILL, STOCK_FILL, {"account": "U0000001"}])
    assert [[r["exec_id"] for r in rows] for rows in sink.executions] == [["0000a.0001.01.01"]]


def test_no_row_is_ever_written_without_an_account(monkeypatch: pytest.MonkeyPatch) -> None:
    fills = [STOCK_FILL, OPTION_FILL, {**STOCK_FILL, "exec_id": "0000c.0003.01.01", "account": None}]
    sink = _refresh(monkeypatch, fills)
    assert all(r.get("account_id") for rows in sink.executions for r in rows)


def test_broker_writes_off_still_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _refresh(monkeypatch, [STOCK_FILL], writes_off=True).executions == []
