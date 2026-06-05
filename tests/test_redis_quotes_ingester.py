"""Tests for IB ingestor quote paths (subscribe_channel, get_ingester_tick)."""

from unittest.mock import MagicMock

from bifrost_core.core.realtime.redis_keys import PUB_CHANNEL, SUBSCRIBE_CHANNEL_DEFAULT
from bifrost_core.core.realtime.redis_quotes import (
    RedisQuotesReader,
    RedisRealtimeParams,
    parse_redis_realtime_params,
)


def test_parse_redis_realtime_subscribe_channel_defaults() -> None:
    cfg = {"redis": {"enabled": True, "host": "127.0.0.1", "port": 6379}}
    p = parse_redis_realtime_params(cfg)
    assert p is not None
    assert p.subscribe_channel == SUBSCRIBE_CHANNEL_DEFAULT
    assert p.channel == PUB_CHANNEL


def test_parse_redis_realtime_subscribe_channel_override() -> None:
    cfg = {
        "redis": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 6379,
            "subscribe_channel": "custom:quotes",
        }
    }
    p = parse_redis_realtime_params(cfg)
    assert p is not None
    assert p.subscribe_channel == "custom:quotes"


def test_get_quote_prefers_ingester_tick_over_legacy_quote_key() -> None:
    p = RedisRealtimeParams(
        host="127.0.0.1",
        port=6379,
        db=0,
        password=None,
        socket_connect_timeout=5.0,
        quote_ttl_sec=300,
        channel=PUB_CHANNEL,
        subscribe_channel=SUBSCRIBE_CHANNEL_DEFAULT,
    )
    r = RedisQuotesReader(p)
    r._client = MagicMock()
    r._available = True
    ing_val = '{"symbol":"AAPL","contract_key":"AAPL|STK|||","ts":2.0,"bid":101.0}'
    r._client.get.side_effect = lambda k: ing_val if k == "ib:ingester:tick:AAPL|STK|||" else None
    out = r.get_quote("AAPL")
    assert out is not None
    assert out["bid"] == 101.0
    r._client.get.assert_called_with("ib:ingester:tick:AAPL|STK|||")


def test_get_ingester_tick_reads_and_parses() -> None:
    p = RedisRealtimeParams(
        host="127.0.0.1",
        port=6379,
        db=0,
        password=None,
        socket_connect_timeout=5.0,
        quote_ttl_sec=300,
        channel=PUB_CHANNEL,
        subscribe_channel=SUBSCRIBE_CHANNEL_DEFAULT,
    )
    r = RedisQuotesReader(p)
    r._client = MagicMock()
    r._available = True
    ck = "AAPL|STK|||"
    r._client.get.return_value = '{"symbol":"AAPL","contract_key":"AAPL|STK|||","ts":1.5,"bid":100.0}'
    out = r.get_ingester_tick(ck)
    assert out is not None
    assert out["symbol"] == "AAPL"
    assert out["bid"] == 100.0
    r._client.get.assert_called_once_with("ib:ingester:tick:" + ck)
