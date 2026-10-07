"""A failed write is logged at WARNING or above, never at DEBUG (TD-216).

The daemon's ``[ib_edge] write_open_orders`` / ``write_account_executions`` failures were logged at
debug, so with or without a configured logger nothing reached kubectl logs or Loki.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_SRC = Path(__file__).resolve().parents[1] / "src" / "bifrost_core"


def _debug_write_failures() -> list[str]:
    """``logger.debug(...)`` inside an ``except`` block whose message names a write."""
    hits = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            for node in ast.walk(handler):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "debug"
                ):
                    continue
                texts = [
                    a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
                ]
                if any("write" in t.lower() for t in texts):
                    hits.append(f"{path.relative_to(_SRC.parent)}:{node.lineno}")
    return hits


def test_no_write_failure_is_logged_at_debug():
    assert _debug_write_failures() == []


class _Sink:
    def write_open_orders(self, orders):
        raise RuntimeError("connection already closed")

    def write_account_executions(self, rows):
        raise RuntimeError("connection already closed")


def test_ib_edge_write_failures_reach_warning(monkeypatch, caplog):
    from bifrost_core.portfolio import ib_edge

    snapshot = {"accounts_snapshot": [], "open_orders": [], "last_execution_rows": [{"x": 1}]}
    r = MagicMock()
    r.get.return_value = json.dumps(snapshot)
    monkeypatch.setattr(ib_edge, "_redis_sync_client", lambda cfg: r)
    monkeypatch.setattr(ib_edge, "daemon_broker_writes_off", lambda: False)
    app = SimpleNamespace(
        config={},
        store=MagicMock(),
        _host_account_id=None,
        _status_sink=_Sink(),
        symbol="",
    )
    with caplog.at_level(logging.DEBUG, logger="bifrost_core.portfolio.ib_edge"):
        asyncio.run(ib_edge.refresh_accounts_from_redis_edge(app))
    failures = {
        rec.getMessage().split(":")[0]: rec.levelno
        for rec in caplog.records
        if "connection already closed" in rec.getMessage()
    }
    assert failures == {
        "[ib_edge] write_open_orders": logging.WARNING,
        "[ib_edge] write_account_executions": logging.WARNING,
    }


def test_missing_open_orders_key_is_not_an_empty_book(monkeypatch):
    """TD-211: no open_orders key must not call write_open_orders (that TRUNCATEs)."""
    from bifrost_core.portfolio import ib_edge

    calls: list = []

    class _Recording:
        def write_open_orders(self, orders):
            calls.append(list(orders))

        def write_account_executions(self, rows):
            calls.append(("exec", rows))

    r = MagicMock()
    r.get.return_value = json.dumps({"accounts_snapshot": []})
    monkeypatch.setattr(ib_edge, "_redis_sync_client", lambda cfg: r)
    monkeypatch.setattr(ib_edge, "daemon_broker_writes_off", lambda: False)
    app = SimpleNamespace(
        config={},
        store=MagicMock(),
        _host_account_id=None,
        _status_sink=_Recording(),
        symbol="",
    )
    asyncio.run(ib_edge.refresh_accounts_from_redis_edge(app))
    assert calls == []


def test_snapshot_applied_is_info(monkeypatch, caplog):
    """The per-refresh summary stays INFO: visible once the daemon configures logging."""
    from bifrost_core.portfolio import ib_edge

    r = MagicMock()
    r.get.return_value = json.dumps({"accounts_snapshot": [], "open_orders": []})
    monkeypatch.setattr(ib_edge, "_redis_sync_client", lambda cfg: r)
    monkeypatch.setattr(ib_edge, "daemon_broker_writes_off", lambda: True)
    app = SimpleNamespace(config={}, store=MagicMock(), _host_account_id=None, _status_sink=None, symbol="")
    with caplog.at_level(logging.DEBUG, logger="bifrost_core.portfolio.ib_edge"):
        asyncio.run(ib_edge.refresh_accounts_from_redis_edge(app))
    applied = [rec for rec in caplog.records if "snapshot applied" in rec.getMessage()]
    assert [rec.levelno for rec in applied] == [logging.INFO]
