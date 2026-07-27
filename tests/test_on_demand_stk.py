"""Tests for on-demand STK Redis helpers."""

from __future__ import annotations

from typing import Any, Dict, List, Set

from bifrost_core.core.realtime.ib_ingestor_keys import (
    IB_INGESTER_ON_DEMAND_STK,
    IB_INGESTER_ON_DEMAND_STK_TS,
)
from bifrost_core.core.realtime.on_demand_stk import (
    ensure_on_demand_stk,
    list_fresh_on_demand_stk,
    normalize_stk_symbols,
)


class _FakePipe:
    def __init__(self, store: "_FakeRedis") -> None:
        self._store = store
        self._ops: List[tuple] = []

    def sadd(self, key: str, *members: str) -> "_FakePipe":
        self._ops.append(("sadd", key, members))
        return self

    def hset(self, key: str, mapping: Dict[str, str] | None = None, **kwargs: Any) -> "_FakePipe":
        self._ops.append(("hset", key, mapping or kwargs))
        return self

    def execute(self) -> List[Any]:
        out: List[Any] = []
        for op in self._ops:
            if op[0] == "sadd":
                _, key, members = op
                out.append(self._store.sadd(key, *members))
            elif op[0] == "hset":
                _, key, mapping = op
                out.append(self._store.hset(key, mapping=mapping))
        self._ops.clear()
        return out


class _FakeRedis:
    def __init__(self) -> None:
        self.sets: Dict[str, Set[str]] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}

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
