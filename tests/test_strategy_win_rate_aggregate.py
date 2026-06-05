"""Win Rate P&L aggregation is structure-agnostic: only grouping differs by ``strategy_structure_name``."""

import pytest

from bifrost_core.monitor.reader.strategy_win_rate import _aggregate_win_rate_metrics


def _row(net: float, underlying: float = 1.0) -> dict:
    return {"net_pnl": net, "underlying_cost": underlying}


def test_aggregate_pnl_identical_for_different_structure_labels():
    """Same instance nets must yield same total_profit / total_loss regardless of card name."""
    rows = [_row(100.0, 10.0), _row(-50.0, 20.0), _row(25.5, 5.0)]
    a = _aggregate_win_rate_metrics("Iron Condor", rows)
    b = _aggregate_win_rate_metrics("Bull Put Spread", rows)
    c = _aggregate_win_rate_metrics("Cash Secured Put", rows)

    for k in ("total_profit", "total_loss", "profit_trades", "loss_trades", "total_instances"):
        assert a[k] == b[k] == c[k]

    assert a["total_profit"] == 125.5
    assert a["total_loss"] == -50.0
    assert a["structure_name"] == "Iron Condor"
    assert b["structure_name"] == "Bull Put Spread"
    assert c["structure_name"] == "Cash Secured Put"


def test_all_winners_total_loss_none():
    """Single strictly positive instance: no neg nets → total_loss None, loss_trades 0."""
    rows = [_row(549.9, 1.0)]
    r = _aggregate_win_rate_metrics("Bear Call Spread", rows)
    assert r["total_profit"] == 549.9
    assert r["total_loss"] is None
    assert r["profit_trades"] == 1
    assert r["loss_trades"] == 0


def test_breakeven_counts_as_loss_bucket_but_not_in_total_loss_sum():
    """net == 0 is in loss bucket for counts / averages but does not add to total_loss (strictly < 0)."""
    rows = [_row(0.0, 1.0), _row(-10.0, 2.0)]
    r = _aggregate_win_rate_metrics("Any", rows)
    assert r["profit_trades"] == 0
    assert r["loss_trades"] == 2
    assert r["total_loss"] == -10.0
    assert r["total_profit"] == 0


def test_all_losers_total_profit_zero():
    rows = [_row(-100.0, 1.0), _row(-0.01, 2.0)]
    r = _aggregate_win_rate_metrics("Any", rows)
    assert r["total_profit"] == 0
    assert r["total_loss"] == pytest.approx(-100.01)
