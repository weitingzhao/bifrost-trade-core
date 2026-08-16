"""Unit tests: index refresh enqueues Plugin stock_daily (no src.massive)."""

from __future__ import annotations

from unittest.mock import patch

from bifrost_core.monitor.integrations import index_data_client as idc


def test_refresh_reference_indices_enqueues_plugin_stock_daily() -> None:
    cfg = {
        "reference_indices": [
            {"symbol": "SPY", "label": "S&P 500", "polygon_ticker": "SPY"},
            {"symbol": "QQQ", "polygon_ticker": "QQQ"},
        ]
    }
    with patch.object(
        idc,
        "_run_one_index_plugin",
        side_effect=[(True, "SPY", ""), (True, "QQQ", "")],
    ) as run:
        with patch.object(idc.time, "sleep"):
            out = idc.refresh_reference_indices(cfg, delay_sec=0)
    assert out == {"ok": True, "updated": ["SPY", "QQQ"], "errors": []}
    assert run.call_count == 2


def test_refresh_one_index_uses_plugin_write_client() -> None:
    cfg = {"reference_indices": [{"symbol": "IWM", "polygon_ticker": "IWM"}]}
    with patch(
        "bifrost_core.monitor.market_write_client.post_ingest_enqueue",
        return_value={"ok": True, "job_id": "42", "kind": "stock_daily"},
    ) as post:
        out = idc.refresh_one_index(cfg, "IWM", days=10)
    assert out["ok"] is True
    assert out["updated"] == ["IWM"]
    kind, payload = post.call_args.args
    assert kind == "stock_daily"
    assert payload["symbol"] == "IWM"
    assert "from" in payload and "to" in payload


def test_caret_symbol_without_polygon_ticker_errors() -> None:
    cfg = {"reference_indices": [{"symbol": "^GSPC", "label": "S&P"}]}
    out = idc.refresh_one_index(cfg, "^GSPC")
    assert out["ok"] is False
    assert out["errors"]
    assert "polygon_ticker" in out["errors"][0]
