"""Tests for unified IB Broker socket status assembly."""

from __future__ import annotations

import time

from bifrost_core.monitor.integrations.ib_socket_status import (
    build_ib_socket_status,
    ib_slot_probe_unhealthy,
    rollup_ib_broker_lamp,
    secondary_slot_configured,
)

_NOW = 1_700_000_000.0
_IB_CFG = {
    "client_id_ib_ingestor": 50,
    "client_id_account_agent": 60,
    "ib2_client_id_account_agent": 61,
    "client_id_operator": 20,
    "ib2_client_id_operator": 21,
    "ib2_host": "10.0.0.2",
}


def test_ingestor_builds_host_slot_and_top_level_mirror() -> None:
    h = {
        "connected": "1",
        "client_id": "50",
        "reconnects": "2",
        "msg_count": "100",
        "last_msg_ts": str(_NOW - 3),
        "ib_probe_at": str(_NOW - 2),
        "ib_probe_ok": "1",
        "ib_probe_interval_sec": "15",
        "service_heartbeat_interval_sec": "30",
        "last_service_heartbeat_at": str(_NOW - 5),
    }
    out = build_ib_socket_status("ib_ingestor", h, _IB_CFG, now=_NOW)
    assert out["connected"] is True
    assert out["host"]["client_id"] == 50
    assert out["host"]["last_ib_probe_at"] == _NOW - 2
    assert out["secondary"] is None
    assert out["last_ib_probe_at"] == _NOW - 2
    assert out["client_id"] == 50


def test_account_agent_reads_canonical_probe_keys() -> None:
    h = {
        "host_connected": "1",
        "host_client_id": "60",
        "host_reconnects": "1",
        "host_ib_probe_at": str(_NOW - 2),
        "host_ib_probe_ok": "1",
        "host_ib_probe_interval_sec": "15",
        "secondary_present": "1",
        "secondary_connected": "1",
        "secondary_client_id": "61",
        "secondary_ib_probe_at": str(_NOW - 2),
        "secondary_ib_probe_ok": "1",
        "secondary_ib_probe_interval_sec": "15",
        "host_alive": "1",
        "msg_count": "10",
        "last_msg_ts": str(_NOW - 1),
    }
    out = build_ib_socket_status("ib_account_agent", h, _IB_CFG, now=_NOW)
    assert out["host"]["last_ib_probe_at"] == _NOW - 2
    assert out["secondary"] is not None
    assert out["secondary"]["client_id"] == 61
    assert out["secondary"]["last_ib_probe_at"] == _NOW - 2


def test_account_agent_reads_legacy_probe_keys() -> None:
    h = {
        "host_connected": "1",
        "host_client_id": "60",
        "host_probe_at": str(_NOW - 2),
        "host_probe_ok": "1",
        "host_probe_interval_sec": "15",
        "secondary_connected": "1",
        "secondary_client_id": "61",
        "secondary_probe_at": str(_NOW - 2),
        "secondary_probe_ok": "1",
        "secondary_probe_interval_sec": "15",
        "last_msg_ts": str(_NOW - 1),
    }
    out = build_ib_socket_status("ib_account_agent", h, _IB_CFG, now=_NOW)
    assert out["host"]["last_ib_probe_at"] == _NOW - 2
    assert out["secondary"] is not None
    assert out["secondary"]["last_ib_probe_at"] == _NOW - 2


def test_operator_build_from_flat_hash() -> None:
    h = {
        "host_connected": "1",
        "host_client_id": "20",
        "host_reconnects": "0",
        "host_ib_probe_at": str(_NOW - 2),
        "host_ib_probe_ok": "1",
        "host_ib_probe_interval_sec": "15",
        "secondary_present": "1",
        "secondary_connected": "1",
        "secondary_client_id": "21",
        "secondary_ib_probe_at": str(_NOW - 2),
        "secondary_ib_probe_ok": "1",
        "secondary_ib_probe_interval_sec": "15",
        "host_alive": "1",
        "msg_count": "0",
        "last_msg_ts": str(_NOW - 2),
    }
    out = build_ib_socket_status("ib_operator", h, _IB_CFG, now=_NOW)
    assert out["connected"] is True
    assert out["service_alive"] is True
    assert out["host"]["client_id"] == 20
    assert out["secondary"] is not None


def test_rollup_lamp_host_probe_stale_is_red() -> None:
    block = {
        "connected": True,
        "service_alive": True,
        "last_msg_age_s": 5.0,
        "host": {"connected": True, "ib_probe_stale": True},
        "secondary": {"connected": True},
    }
    assert rollup_ib_broker_lamp(block)["lamp"] == "red"


def test_rollup_lamp_sec_down_is_yellow() -> None:
    block = {
        "connected": True,
        "service_alive": True,
        "last_msg_age_s": 5.0,
        "host": {"connected": True},
        "secondary": {"connected": False},
    }
    assert rollup_ib_broker_lamp(block)["lamp"] == "yellow"


def test_ib_slot_probe_unhealthy_ok_false() -> None:
    assert ib_slot_probe_unhealthy({"last_ib_probe_at": _NOW, "ib_probe_ok": False}) is True


def test_secondary_configured_operator() -> None:
    assert secondary_slot_configured("ib_operator", _IB_CFG) is True
