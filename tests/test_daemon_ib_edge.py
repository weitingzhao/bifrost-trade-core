"""Daemon ib_connected derivation from Socket IB Redis health."""

from __future__ import annotations

from unittest.mock import MagicMock

from bifrost_core.monitor.integrations.daemon_ib_edge import (
    derive_daemon_ib_heartbeat_from_redis,
)

_NOW = 1_700_000_000.0
_IB_CFG = {
    "client_id_ib_ingestor": 50,
    "client_id_account_agent": 60,
    "ib2_client_id_account_agent": 61,
    "client_id_operator": 20,
    "ib2_client_id_operator": 21,
    "ib2_host": "10.0.0.2",
    "ib_probe_stale_multiplier": 2.5,
}


def _healthy_hashes(now: float) -> dict[str, dict[str, str]]:
    ts = str(now - 2)
    lmsg = str(now - 1)
    return {
        "bifrost:health:ws_ib_ingestor": {
            "connected": "1",
            "client_id": "50",
            "reconnects": "0",
            "msg_count": "1",
            "last_msg_ts": lmsg,
            "ib_probe_at": ts,
            "ib_probe_ok": "1",
            "ib_probe_interval_sec": "5",
        },
        "bifrost:health:ws_ib_operator": {
            "host_connected": "1",
            "host_client_id": "20",
            "host_alive": "1",
            "host_reconnects": "0",
            "host_ib_probe_at": ts,
            "host_ib_probe_ok": "1",
            "host_ib_probe_interval_sec": "5",
            "secondary_present": "1",
            "secondary_connected": "1",
            "secondary_client_id": "21",
            "secondary_ib_probe_at": ts,
            "secondary_ib_probe_ok": "1",
            "secondary_ib_probe_interval_sec": "5",
            "msg_count": "0",
            "last_msg_ts": lmsg,
        },
        "bifrost:health:ws_ib_account_agent": {
            "host_connected": "1",
            "host_client_id": "60",
            "host_alive": "1",
            "host_reconnects": "0",
            "host_ib_probe_at": ts,
            "host_ib_probe_ok": "1",
            "host_ib_probe_interval_sec": "5",
            "secondary_present": "1",
            "secondary_connected": "1",
            "secondary_client_id": "61",
            "secondary_ib_probe_at": ts,
            "secondary_ib_probe_ok": "1",
            "secondary_ib_probe_interval_sec": "5",
            "msg_count": "1",
            "last_msg_ts": lmsg,
        },
    }


def test_derive_ib_connected_true_when_all_socket_services_green() -> None:
    data = _healthy_hashes(_NOW)

    def _hgetall(key: str) -> dict:
        return dict(data.get(key, {}))

    r = MagicMock()
    r.hgetall.side_effect = _hgetall
    out = derive_daemon_ib_heartbeat_from_redis(r, _IB_CFG, now=_NOW)
    assert out["ib_connected"] is True
    assert out["ib_client_id"] == 20


def test_derive_ib_connected_false_when_operator_host_down() -> None:
    data = _healthy_hashes(_NOW)
    data["bifrost:health:ws_ib_operator"]["host_connected"] = "0"

    def _hgetall(key: str) -> dict:
        return dict(data.get(key, {}))

    r = MagicMock()
    r.hgetall.side_effect = _hgetall
    out = derive_daemon_ib_heartbeat_from_redis(r, _IB_CFG, now=_NOW)
    assert out["ib_connected"] is False
    assert out["ib_client_id"] is None
