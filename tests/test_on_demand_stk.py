"""Tests for on-demand STK Redis helpers."""

from __future__ import annotations

from typing import Any, Dict, List, Set

from bifrost_core.core.realtime.ib_ingestor_keys import (
    IB_INGESTER_ON_DEMAND_STK,
    IB_INGESTER_ON_DEMAND_STK_TS,
    IB_INGESTER_TICK_PREFIX,
)
from bifrost_core.core.realtime.on_demand_stk import (
    ensure_on_demand_stk,
    list_fresh_on_demand_stk,
    normalize_stk_symbols,
    remove_on_demand_stk,
)


class _FakePipe:
    def __init__(self, store: "_FakeRedis") -> None:
        self._store = store
        self._ops: List[tuple] = []

    def sadd(self, key: str, *members: str) -> "_FakePipe":
        self._ops.append(("sadd", key, members))
        return self

    def srem(self, key: str, *members: str) -> "_FakePipe":
        self._ops.append(("srem", key, members))
        return self

    def hset(self, key: str, mapping: Dict[str, str] | None = None, **kwargs: Any) -> "_FakePipe":
        self._ops.append(("hset", key, mapping or kwargs))
        return self

    def hdel(self, key: str, *fields: str) -> "_FakePipe":
        self._ops.append(("hdel", key, fields))
        return self

    def delete(self, *keys: str) -> "_FakePipe":
        self._ops.append(("delete", keys))
        return self

    def execute(self) -> List[Any]:
        out: List[Any] = []
        for op in self._ops:
            if op[0] == "sadd":
                _, key, members = op
                out.append(self._store.sadd(key, *members))
            elif op[0] == "srem":
                _, key, members = op
                out.append(self._store.srem(key, *members))
            elif op[0] == "hset":
                _, key, mapping = op
                out.append(self._store.hset(key, mapping=mapping))
            elif op[0] == "hdel":
                _, key, fields = op
                out.append(self._store.hdel(key, *fields))
            elif op[0] == "delete":
                _, keys = op
                out.append(self._store.delete(*keys))
        self._ops.clear()
        return out


class _FakeRedis:
    def __init__(self) -> None:
        self.sets: Dict[str, Set[str]] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.kv: Dict[str, Any] = {}

    def pipeline(self, transaction: bool = False) -> _FakePipe:
        return _FakePipe(self)

    def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(str(m) for m in members)
        return len(bucket) - before

    def smembers(self, key: str) -> Set[str]:
        return set(self.sets.get(key, set()))

    def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(key)
        if not bucket:
            return 0
        n = 0
        for m in members:
            if str(m) in bucket:
                bucket.discard(str(m))
                n += 1
        return n

    def hset(self, key: str, mapping: Dict[str, str] | None = None, **kwargs: Any) -> int:
        h = self.hashes.setdefault(key, {})
        data = mapping or {k: str(v) for k, v in kwargs.items()}
        for k, v in data.items():
            h[str(k)] = str(v)
        return len(data)

    def hgetall(self, key: str) -> Dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hdel(self, key: str, *fields: str) -> int:
        h = self.hashes.get(key)
        if not h:
            return 0
        n = 0
        for f in fields:
            if str(f) in h:
                del h[str(f)]
                n += 1
        return n

    def delete(self, *keys: str) -> int:
        n = 0
        for k in keys:
            key = str(k)
            if key in self.kv:
                del self.kv[key]
                n += 1
            elif key in self.sets:
                del self.sets[key]
                n += 1
            elif key in self.hashes:
                del self.hashes[key]
                n += 1
        return n

    def set(self, key: str, value: Any) -> bool:
        self.kv[str(key)] = value
        return True

    def get(self, key: str) -> Any:
        return self.kv.get(str(key))


def test_normalize_stk_symbols_dedupes_and_skips_contract_keys() -> None:
    assert normalize_stk_symbols(["aapl", "AAPL", "GOOG|OPT|1|2|C", "  sgov "]) == [
        "AAPL",
        "SGOV",
    ]


def test_ensure_on_demand_stk_sadd_and_heartbeat() -> None:
    r = _FakeRedis()
    out = ensure_on_demand_stk(r, ["sgov", "GOOG", "sgov"], now=1_000.0)
    assert out == ["SGOV", "GOOG"]
    assert r.sets[IB_INGESTER_ON_DEMAND_STK] == {"SGOV", "GOOG"}
    assert r.hashes[IB_INGESTER_ON_DEMAND_STK_TS]["SGOV"] == "1000.0"
    assert r.hashes[IB_INGESTER_ON_DEMAND_STK_TS]["GOOG"] == "1000.0"


def test_ensure_on_demand_stk_empty() -> None:
    r = _FakeRedis()
    assert ensure_on_demand_stk(r, []) == []
    assert ensure_on_demand_stk(None, ["AAPL"]) == []


def test_list_fresh_prunes_stale() -> None:
    r = _FakeRedis()
    ensure_on_demand_stk(r, ["SGOV", "GOOG"], now=1_000.0)
    # Refresh only SGOV
    ensure_on_demand_stk(r, ["SGOV"], now=1_050.0)
    fresh = list_fresh_on_demand_stk(r, max_age_sec=30, now=1_060.0)
    assert fresh == ["SGOV"]
    assert "GOOG" not in r.sets[IB_INGESTER_ON_DEMAND_STK]
    assert "GOOG" not in r.hashes[IB_INGESTER_ON_DEMAND_STK_TS]


def test_remove_on_demand_stk_srem_hdel_and_tick() -> None:
    r = _FakeRedis()
    ensure_on_demand_stk(r, ["SGOV", "GOOG", "AAPL"], now=1_000.0)
    tick_goog = f"{IB_INGESTER_TICK_PREFIX}GOOG|STK|||"
    tick_sgov = f"{IB_INGESTER_TICK_PREFIX}SGOV|STK|||"
    r.set(tick_goog, '{"last":1}')
    r.set(tick_sgov, '{"last":2}')

    # normalize dedupes goog/GOOG; MISSING is still a valid ticker to process
    n = remove_on_demand_stk(r, ["goog", "GOOG", "MISSING"])
    assert n == 2
    assert "GOOG" not in r.sets[IB_INGESTER_ON_DEMAND_STK]
    assert "GOOG" not in r.hashes[IB_INGESTER_ON_DEMAND_STK_TS]
    assert tick_goog not in r.kv
    assert "SGOV" in r.sets[IB_INGESTER_ON_DEMAND_STK]
    assert tick_sgov in r.kv
    assert "AAPL" in r.sets[IB_INGESTER_ON_DEMAND_STK]


def test_remove_on_demand_stk_empty() -> None:
    r = _FakeRedis()
    assert remove_on_demand_stk(r, []) == 0
    assert remove_on_demand_stk(None, ["AAPL"]) == 0
