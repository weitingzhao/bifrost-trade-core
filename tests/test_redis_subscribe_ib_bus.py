"""SSE subscribe prefers redis_ib when configured."""

from __future__ import annotations

from bifrost_core.core.realtime.redis_quotes import RedisQuotesReader, RedisRealtimeParams
from bifrost_core.core.realtime.redis_subscribe import _subscribe_connection_kwargs


def test_subscribe_kwargs_prefer_redis_ib(monkeypatch) -> None:
    for key in (
        "REDIS_IB_HOST",
        "REDIS_IB_PORT",
        "REDIS_IB_PASSWORD",
        "REDIS_IB_USERNAME",
        "REDIS_IB_DB",
    ):
        monkeypatch.delenv(key, raising=False)
    params = RedisRealtimeParams(
        host="redis",
        port=6379,
        db=0,
        password=None,
        socket_connect_timeout=5.0,
        quote_ttl_sec=300,
        channel="unused",
        subscribe_channel="ib:ingester:channel",
    )
    reader = RedisQuotesReader(
        params,
        {
            "redis": {"enabled": True, "host": "redis"},
            "redis_ib": {
                "enabled": True,
                "host": "redis-ib",
                "port": 6379,
                "username": "trade-prod",
                "password": "secret",
                "db": 0,
            },
        },
    )
    kw = _subscribe_connection_kwargs(reader)
    assert kw["via"] == "redis_ib"
    assert kw["host"] == "redis-ib"
    assert kw["username"] == "trade-prod"
    assert kw["password"] == "secret"
    assert kw["subscribe_channel"] == "ib:ingester:channel"


def test_subscribe_kwargs_fallback_live_redis(monkeypatch) -> None:
    for key in (
        "REDIS_IB_HOST",
        "REDIS_IB_PORT",
        "REDIS_IB_PASSWORD",
        "REDIS_IB_USERNAME",
        "REDIS_IB_DB",
    ):
        monkeypatch.delenv(key, raising=False)
    params = RedisRealtimeParams(
        host="redis-live",
        port=6379,
        db=0,
        password="p",
        socket_connect_timeout=5.0,
        quote_ttl_sec=300,
        channel="unused",
        subscribe_channel="ib:ingester:channel",
    )
    reader = RedisQuotesReader(params, {"redis": {"enabled": True, "host": "redis-live"}})
    kw = _subscribe_connection_kwargs(reader)
    assert kw["via"] == "redis"
    assert kw["host"] == "redis-live"
