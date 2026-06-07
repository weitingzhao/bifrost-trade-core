"""IB socket service stop → message center publish helpers."""

from __future__ import annotations

from bifrost_core.core.message_center import (
    IB_DISCONNECT_REASON_SERVICE_STOPPED,
    publish_ib_connection_transition,
    publish_ib_service_stopped_messages,
)


class FakeRedis:
    def __init__(self) -> None:
        self.stream: list[tuple[str, dict[str, str]]] = []

    def xadd(self, key: str, fields: dict[str, str], maxlen=None, approximate=None) -> str:
        entry_id = f"{len(self.stream) + 1}-0"
        self.stream.append((entry_id, {"_stream_key": key, **fields}))
        return entry_id


def test_publish_ib_connection_transition_disconnected() -> None:
    redis = FakeRedis()
    publish_ib_connection_transition(
        redis,
        service="ib_ingestor",
        slot="host",
        client_id=50,
        status_from="connected",
        status_to="disconnected",
        reason=IB_DISCONNECT_REASON_SERVICE_STOPPED,
        occurred_at=100.0,
    )
    assert len(redis.stream) == 1
    _eid, payload = redis.stream[0]
    assert payload["service"] == "ib_ingestor"
    assert payload["status_to"] == "disconnected"
    assert payload["reason"] == IB_DISCONNECT_REASON_SERVICE_STOPPED


def test_publish_ib_service_stopped_messages_operator_host_alive_only() -> None:
    """Stop while IB disconnected but service still alive must still emit HOST toast."""
    redis = FakeRedis()
    publish_ib_service_stopped_messages(
        redis,
        service_id="ib_operator",
        health_hash={
            "host_connected": "0",
            "host_alive": "1",
            "host_client_id": "20",
            "secondary_present": "1",
            "secondary_connected": "0",
            "secondary_client_id": "21",
        },
        occurred_at=150.0,
    )
    assert len(redis.stream) == 2
    slots = {p["slot"] for _, p in redis.stream}
    assert slots == {"host", "secondary"}


def test_publish_ib_service_stopped_messages_operator_dual_slot() -> None:
    redis = FakeRedis()
    publish_ib_service_stopped_messages(
        redis,
        service_id="ib_operator",
        health_hash={
            "host_connected": "1",
            "host_client_id": "20",
            "secondary_connected": "1",
            "secondary_client_id": "21",
        },
        occurred_at=200.0,
    )
    assert len(redis.stream) == 2
    slots = {p["slot"] for _, p in redis.stream}
    assert slots == {"host", "secondary"}
    for _eid, payload in redis.stream:
        assert payload["status_to"] == "disconnected"
        assert payload["reason"] == IB_DISCONNECT_REASON_SERVICE_STOPPED
