"""Platform IB Gateway health derivation tests."""

from __future__ import annotations

from unittest.mock import MagicMock

from bifrost_core.monitor.integrations.platform_ib_gateway import (
    build_platform_ib_gateway_status,
    build_ib_socket_blocks_from_redis,
    derive_daemon_ib_heartbeat_from_redis,
    detect_ib_transport,
    is_platform_ib_gateway_health,
    rollup_platform_ib_gateway_lamp,
)
from bifrost_core.monitor.integrations.ib_socket_status import build_ib_socket_status

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


def _gateway_hashes(now: float) -> dict[str, dict[str, str]]:
    ts = str(now - 2)
    lmsg = str(now - 1)
    base = {"plugin": "ib-gateway", "mode": "mock", "last_msg_ts": lmsg}
    return {
        "bifrost:health:ws_ib_ingestor": {
            **base,
            "connected": "1",
            "client_id": "50",
            "reconnects": "0",
            "msg_count": "1",
            "ib_probe_at": ts,
            "ib_probe_ok": "1",
            "ib_probe_interval_sec": "5",
        },
        "bifrost:health:ws_ib_operator": {
            **base,
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
        },
        "bifrost:health:ws_ib_account_agent": {
            **base,
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
        },
    }


def test_is_platform_ib_gateway_health_by_plugin() -> None:
    assert is_platform_ib_gateway_health({"plugin": "ib-gateway", "mode": "live"}) is True
    assert is_platform_ib_gateway_health({"mode": "mock"}) is True
    assert is_platform_ib_gateway_health({"connected": "1"}) is False


def test_detect_ib_transport_platform() -> None:
    h = {"plugin": "ib-gateway", "mode": "mock"}
    assert detect_ib_transport(h, {}, {}) == "platform_gateway"
    assert detect_ib_transport({}, {}, {}) == "legacy_socket"


def test_derive_daemon_ib_heartbeat_platform_gateway() -> None:
    data = _gateway_hashes(_NOW)

    def _hgetall(key: str) -> dict:
        return dict(data.get(key, {}))

    r = MagicMock()
    r.hgetall.side_effect = _hgetall
    out = derive_daemon_ib_heartbeat_from_redis(r, _IB_CFG, now=_NOW)
    assert out["ib_connected"] is True
    assert out["ib_client_id"] == 20
    assert out["ib_transport"] == "platform_gateway"
    assert out["platform_ib_gateway"]["mode"] == "mock"
    assert out["platform_ib_gateway"]["lamp"] == "green"


def test_build_platform_ib_gateway_status_none_for_legacy() -> None:
    h = {"connected": "1", "client_id": "50", "last_msg_ts": str(_NOW)}
    ingestor = build_ib_socket_status("ib_ingestor", h, _IB_CFG, now=_NOW)
    account = build_ib_socket_status("ib_account_agent", h, _IB_CFG, now=_NOW)
    operator = build_ib_socket_status("ib_operator", None, _IB_CFG, now=_NOW)
    assert build_platform_ib_gateway_status(
        ingestor, account, operator, transport="legacy_socket"
    ) is None


def test_rollup_platform_ib_gateway_lamp_yellow_when_secondary_down() -> None:
    data = _gateway_hashes(_NOW)
    data["bifrost:health:ws_ib_operator"]["secondary_connected"] = "0"

    def _hgetall(key: str) -> dict:
        return dict(data.get(key, {}))

    r = MagicMock()
    r.hgetall.side_effect = _hgetall
    ingestor, account, operator, _ = build_ib_socket_blocks_from_redis(
        r, _IB_CFG, now=_NOW, stale_mult=2.5
    )
    lamp = rollup_platform_ib_gateway_lamp(ingestor, account, operator)
    assert lamp["lamp"] in ("yellow", "red")
