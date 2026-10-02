"""POST /executions/fetch: IB Gateway plugin fills mapped to the execution writer's row.

The plugin row shape is copied from bifrost-platform-plugin ib_gateway/ib_ops.py
(fetch_executions); values are made up. Before core 0.35.0 the api passed these rows to
write_account_executions_to_db unmapped, so account, quantity, time and source were NULL.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.portfolio.gateway_fills import (
    GATEWAY_FILL_SOURCE,
    execution_row_from_gateway_fill,
    execution_rows_from_gateway_fills,
)
from bifrost_core.portfolio.reader import accounts
from write_fakes import FakeConn, Reply


def _plugin_fill(**over: Any) -> Dict[str, Any]:
    fill = {
        "exec_id": "0000e0d5.66f0a1b2.01.01",
        "account": "U0000003",
        "symbol": "ZZQ",
        "sec_type": "STK",
        "side": "SLD",
        "shares": 25.0,
        "price": 41.25,
        "commission": 1.0,
        "realized_pnl": 12.5,
        "ts": 1_790_000_000.0,
    }
    fill.update(over)
    return fill


def test_plugin_names_become_writer_names() -> None:
    row = execution_row_from_gateway_fill(_plugin_fill())
    assert row == {
        "exec_id": "0000e0d5.66f0a1b2.01.01",
        "account_id": "U0000003",
        "symbol": "ZZQ",
        "sec_type": "STK",
        "side": "SLD",
        "quantity": 25.0,
        "price": 41.25,
        "commission": 1.0,
        "realized_pnl": 12.5,
        "time": 1_790_000_000.0,
        "source": "tws_client",
        "contract_key": "ZZQ|STK|||",
    }
    assert GATEWAY_FILL_SOURCE == "tws_client"  # what the stored TWS rows carry


def test_option_fill_without_its_contract_is_refused() -> None:
    """The plugin sends the underlying symbol and no expiry / strike / right: not written,
    since a keyless row would also block the complete one (ON CONFLICT (exec_id) DO NOTHING)."""
    fill = _plugin_fill(sec_type="OPT", shares=2.0)
    row = execution_row_from_gateway_fill(fill)
    assert row["quantity"] == 2.0 and "contract_key" not in row and "expiry" not in row
    ok, refused = execution_rows_from_gateway_fills([fill])
    assert ok == [] and refused == [
        {"exec_id": "0000e0d5.66f0a1b2.01.01", "missing": ["expiry", "strike", "option_right"]}
    ]


def test_option_fields_pass_through_when_the_plugin_sends_them() -> None:
    row = execution_row_from_gateway_fill(
        _plugin_fill(sec_type="OPT", expiry="20261120", strike=40.0, option_right="C", source="tws_event")
    )
    assert (row["expiry"], row["strike"], row["option_right"], row["source"]) == ("20261120", 40.0, "C", "tws_event")
    assert execution_rows_from_gateway_fills([row])[1] == []


def test_writer_names_win_over_plugin_names() -> None:
    row = execution_row_from_gateway_fill(_plugin_fill(account_id="U0000004", quantity=3.0, time=5.0))
    assert (row["account_id"], row["quantity"], row["time"]) == ("U0000004", 3.0, 5.0)


@pytest.mark.parametrize("drop", ["exec_id", "account", "side", "shares"])
def test_incomplete_fills_are_refused_not_written_as_nulls(drop: str) -> None:
    fill = _plugin_fill()
    fill[drop] = None
    ok, refused = execution_rows_from_gateway_fills([fill, _plugin_fill(exec_id="b")])
    assert [r["exec_id"] for r in ok] == ["b"]
    assert len(refused) == 1 and len(refused[0]["missing"]) == 1
    _, refused = execution_rows_from_gateway_fills(["not a dict"])
    assert refused == [{"exec_id": None, "missing": ["exec_id", "account_id", "side", "quantity"]}]


def test_mapped_row_reaches_the_raw_tws_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn([("INSERT INTO raw_broker.executions_raw_tws", Reply(one=(77,)))])
    monkeypatch.setattr(ws, "open_conn", lambda cfg, golden=False: conn)
    ok, _ = execution_rows_from_gateway_fills([_plugin_fill()])
    stats: Dict[str, Any] = {}
    assert accounts.write_account_executions_to_db({"sink": "postgres"}, ok, stats_out=stats)
    sql, params = conn.statement("INSERT INTO raw_broker.executions_raw_tws")
    cols = [c.strip() for c in sql.split("(", 1)[1].split(")", 1)[0].split(",")]
    got = dict(zip(cols, params))
    assert got["account_id"] == "U0000003"
    assert got["quantity"] == 25.0  # stored unsigned, like every TWS row
    assert got["source"] == "tws_client"
    assert got["exec_time"] is not None and got["exec_time"].timestamp() == 1_790_000_000.0
    assert got["trade_date"] is not None
    assert got["contract_key"] == "ZZQ|STK|||"
    assert stats["tws_raw_inserted_ids"] == [77]
    assert conn.ran("INSERT INTO raw_broker.commissions")
