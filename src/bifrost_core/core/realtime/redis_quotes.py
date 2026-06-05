"""Redis quote cache: optional writer (legacy/other) and reader (daemon, monitor/API). IB Ingestor writes tick keys."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from bifrost_core.core.realtime.ib_ingestor_keys import IB_INGESTER_TICK_PREFIX

from .redis_keys import (
    PUB_CHANNEL,
    QUOTE_KEY_PREFIX,
    QUOTE_TTL_SEC,
    SUBSCRIBE_CHANNEL_DEFAULT,
    TICKER_SUBSCRIBED_KEY,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RedisRealtimeParams:
    """Connection and quote-cache options parsed from app config (redis / realtime sections)."""

    host: str
    port: int
    db: int
    password: Optional[str]
    socket_connect_timeout: float
    quote_ttl_sec: int
    channel: str
    subscribe_channel: str


def get_quote_key(symbol: str) -> str:
    return f"{QUOTE_KEY_PREFIX}{symbol}"


def parse_redis_realtime_params(config: Dict[str, Any]) -> Optional[RedisRealtimeParams]:
    """Parse redis/realtime config. Returns None if quotes feature is disabled (no connect)."""
    redis_cfg = config.get("redis") or {}
    realtime_cfg = config.get("realtime") or {}
    enabled = redis_cfg.get("enabled", False) or realtime_cfg.get("enabled", False)
    if not enabled:
        logger.info(
            "Redis quotes disabled: redis.enabled (or realtime.enabled) not true in config"
        )
        return None
    host = redis_cfg.get("host", "127.0.0.1")
    port = int(redis_cfg.get("port", 6379))
    db = int(redis_cfg.get("db", 0))
    password = redis_cfg.get("password")
    timeout = float(redis_cfg.get("socket_connect_timeout", 5.0))
    ttl = int(redis_cfg.get("quote_ttl_sec", QUOTE_TTL_SEC))
    channel = redis_cfg.get("channel", PUB_CHANNEL)
    subscribe_channel = redis_cfg.get("subscribe_channel", SUBSCRIBE_CHANNEL_DEFAULT)
    return RedisRealtimeParams(
        host=host,
        port=port,
        db=db,
        password=password,
        socket_connect_timeout=timeout,
        quote_ttl_sec=ttl,
        channel=channel,
        subscribe_channel=subscribe_channel,
    )


class RedisQuotesWriter:
    """Thread-safe Redis client for writing quotes, pub/sub notify, and subscription set."""

    def __init__(self, params: RedisRealtimeParams):
        self._params = params
        self._client: Any = None
        self._available = False

    @property
    def realtime_params(self) -> RedisRealtimeParams:
        return self._params

    def connect(self) -> bool:
        if self._client is not None:
            try:
                self._client.ping()
                self._available = True
                return True
            except Exception:
                self._client = None
                self._available = False
        try:
            import redis

            self._client = redis.Redis(
                host=self._params.host,
                port=self._params.port,
                db=self._params.db,
                password=self._params.password if self._params.password else None,
                socket_connect_timeout=self._params.socket_connect_timeout,
                socket_timeout=self._params.socket_connect_timeout,
                decode_responses=True,
            )
            self._client.ping()
            self._available = True
            logger.info(
                "Redis quotes writer connected: host=%s port=%s db=%s channel=%s",
                self._params.host,
                self._params.port,
                self._params.db,
                self._params.channel,
            )
            return True
        except ImportError:
            logger.warning("redis package not installed; realtime quotes disabled")
            self._available = False
            return False
        except Exception as e:
            logger.warning("Redis connect failed: %s", e)
            self._client = None
            self._available = False
            return False

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    def set_quote(self, symbol: str, payload: Dict[str, Any]) -> bool:
        if not self._client:
            return False
        key = get_quote_key(symbol)
        try:
            val = json.dumps(payload)
            pipe = self._client.pipeline()
            pipe.set(key, val, ex=self._params.quote_ttl_sec)
            pipe.execute()
            return True
        except Exception as e:
            logger.warning("Redis set_quote failed symbol=%s: %s", symbol, e)
            return False

    def publish_update(self, symbol: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        if not self._client:
            return False
        msg = (payload or {}).copy()
        msg.setdefault("symbol", symbol)
        msg.setdefault("ts", time.time())
        try:
            self._client.publish(self._params.channel, json.dumps(msg))
            return True
        except Exception as e:
            logger.warning("Redis publish_update failed symbol=%s: %s", symbol, e)
            return False

    def delete_quote(self, symbol: str) -> bool:
        if not self._client:
            return False
        key = get_quote_key(symbol)
        try:
            self._client.delete(key)
            return True
        except Exception as e:
            logger.warning("Redis delete_quote failed symbol=%s: %s", symbol, e)
            return False

    def add_symbol_subscribed(self, symbol: str) -> bool:
        if not self._client:
            return False
        s = (symbol or "").strip()
        if not s:
            return False
        try:
            self._client.sadd(TICKER_SUBSCRIBED_KEY, s)
            return True
        except Exception as e:
            logger.warning("Redis add_symbol_subscribed failed symbol=%s: %s", symbol, e)
            return False

    def remove_symbol_subscribed(self, symbol: str) -> bool:
        if not self._client:
            return False
        s = (symbol or "").strip()
        if not s:
            return False
        try:
            self._client.srem(TICKER_SUBSCRIBED_KEY, s)
            return True
        except Exception as e:
            logger.warning("Redis remove_symbol_subscribed failed symbol=%s: %s", symbol, e)
            return False

    def clear_subscribed_set(self) -> bool:
        if not self._client:
            return False
        try:
            self._client.delete(TICKER_SUBSCRIBED_KEY)
            return True
        except Exception as e:
            logger.warning("Redis clear_subscribed_set failed: %s", e)
            return False

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception as e:
                logger.debug("Redis close: %s", e)
            self._client = None
        self._available = False


class RedisQuotesReader:
    """Thread-safe Redis client for reading quotes and subscription set (monitor / API)."""

    def __init__(self, params: RedisRealtimeParams):
        self._params = params
        self._client: Any = None
        self._available = False

    @property
    def realtime_params(self) -> RedisRealtimeParams:
        return self._params

    def connect(self) -> bool:
        if self._client is not None:
            try:
                self._client.ping()
                self._available = True
                return True
            except Exception:
                self._client = None
                self._available = False
        try:
            import redis

            self._client = redis.Redis(
                host=self._params.host,
                port=self._params.port,
                db=self._params.db,
                password=self._params.password if self._params.password else None,
                socket_connect_timeout=self._params.socket_connect_timeout,
                socket_timeout=self._params.socket_connect_timeout,
                decode_responses=True,
            )
            self._client.ping()
            self._available = True
            logger.info(
                "Redis quotes reader connected: host=%s port=%s db=%s subscribe_channel=%s",
                self._params.host,
                self._params.port,
                self._params.db,
                self._params.subscribe_channel,
            )
            return True
        except ImportError:
            logger.warning("redis package not installed; realtime quotes read disabled")
            self._available = False
            return False
        except Exception as e:
            logger.warning("Redis reader connect failed: %s", e)
            self._client = None
            self._available = False
            return False

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    @property
    def redis_client(self) -> Any:
        """Raw Redis client, or None if not connected. Used for health-hash writes."""
        return self._client

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Latest STK quote: prefer IB Ingestor tick key, then legacy ``quote:{symbol}``."""
        if not self._client:
            return None
        sym = (symbol or "").strip()
        if not sym:
            return None
        ing = self.get_ingester_tick(f"{sym}|STK|||")
        if ing is not None:
            return ing
        key = get_quote_key(sym)
        try:
            val = self._client.get(key)
            if val is None:
                return None
            return json.loads(val)
        except Exception as e:
            logger.debug("Redis get_quote failed symbol=%s: %s", sym, e)
            return None

    def get_quotes(self, symbols: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for s in symbols:
            if not s or not s.strip():
                continue
            q = self.get_quote(s.strip())
            if q is not None:
                out.append(q)
        return out

    def get_ingester_tick(self, contract_key: str) -> Optional[Dict[str, Any]]:
        """Latest quote JSON from IB ingestor (``ib:ingester:tick:{contract_key}``)."""
        if not self._client:
            return None
        ck = (contract_key or "").strip()
        if not ck:
            return None
        key = IB_INGESTER_TICK_PREFIX + ck
        try:
            val = self._client.get(key)
            if val is None:
                return None
            return json.loads(val)
        except Exception as e:
            logger.debug("Redis get_ingester_tick failed contract_key=%s: %s", ck, e)
            return None

    def get_subscribed_symbols(self) -> Set[str]:
        if not self._client:
            return set()
        try:
            members = self._client.smembers(TICKER_SUBSCRIBED_KEY)
            return {str(m).strip() for m in (members or []) if m and str(m).strip()}
        except Exception as e:
            logger.debug("Redis get_subscribed_symbols failed: %s", e)
            return set()

    def get_subscribed_symbols_with_ages_sec(
        self,
    ) -> Tuple[Set[str], Dict[str, Optional[float]]]:
        subscribed = self.get_subscribed_symbols()
        now = time.time()
        ages: Dict[str, Optional[float]] = {}
        for sym in subscribed:
            q = self.get_quote(sym)
            if q is None:
                ages[sym] = None
                continue
            ts = q.get("ts")
            if ts is None:
                ages[sym] = None
                continue
            try:
                t = float(ts)
                if t > 0:
                    ages[sym] = now - t
                else:
                    ages[sym] = None
            except (TypeError, ValueError):
                ages[sym] = None
        return subscribed, ages

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            except Exception as e:
                logger.debug("Redis reader close: %s", e)
            self._client = None
        self._available = False


def create_writer_from_config(config: Dict[str, Any]) -> Optional[RedisQuotesWriter]:
    p = parse_redis_realtime_params(config)
    if not p:
        return None
    w = RedisQuotesWriter(p)
    if w.connect():
        return w
    logger.warning(
        "Redis quotes writer unavailable: connect failed to %s:%s (check Redis is running and redis.host/port in config)",
        p.host,
        p.port,
    )
    return None


def create_reader_from_config(config: Dict[str, Any]) -> Optional[RedisQuotesReader]:
    p = parse_redis_realtime_params(config)
    if not p:
        return None
    r = RedisQuotesReader(p)
    if r.connect():
        return r
    logger.warning(
        "Redis quotes reader unavailable: connect failed to %s:%s (check Redis is running and redis.host/port in config)",
        p.host,
        p.port,
    )
    return None
