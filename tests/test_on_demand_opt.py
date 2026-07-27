"""Tests for on-demand OPT Redis helpers (core)."""

from __future__ import annotations

from typing import Any, Dict, List, Set

from bifrost_core.core.realtime.ib_ingestor_keys import (
    IB_OPTION_CACHE_PREFIX,
    IB_OPTION_ON_DEMAND_SET,
    IB_OPTION_ON_DEMAND_TS,
)
from bifrost_core.core.realtime.on_demand_opt import (
    ensure_on_demand_opt,
    list_fresh_on_demand_opt,
    normalize_opt_contract_keys,
    remove_on_demand_opt,
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


CK_A = "GOOG|OPT|20260717|300.0|C"
CK_B = "AAPL|OPT|20260815|200.0|P"


def test_normalize_opt_contract_keys() -> None:
    assert normalize_opt_contract_keys(
        [
            "goog|opt|20260717|300.0|c",
            CK_A,
            "NVDA|STK|||",
            "BAD|OPT|x|1|C",
            "  aapl|OPT|20260815|200.0|P ",
            "SPY|OPT|20260101|notanumber|C",
        ]
    ) == [CK_A, CK_B]


def test_ensure_on_demand_opt_sadd_and_heartbeat() -> None:
    r = _FakeRedis()
    out = ensure_on_demand_opt(r, [CK_A.lower(), CK_B], now=2_000.0)
    assert out == [CK_A, CK_B]
    assert r.sets[IB_OPTION_ON_DEMAND_SET] == {CK_A, CK_B}
    assert r.hashes[IB_OPTION_ON_DEMAND_TS][CK_A] == "2000.0"


def test_ensure_on_demand_opt_empty() -> None:
    r = _FakeRedis()
    assert ensure_on_demand_opt(r, []) == []
    assert ensure_on_demand_opt(None, [CK_A]) == []


def test_list_fresh_prunes_stale() -> None:
    r = _FakeRedis()
    ensure_on_demand_opt(r, [CK_A, CK_B], now=1_000.0)
    ensure_on_demand_opt(r, [CK_A], now=1_050.0)
    fresh = list_fresh_on_demand_opt(r, max_age_sec=30, now=1_060.0)
    assert fresh == [CK_A]
    assert CK_B not in r.sets[IB_OPTION_ON_DEMAND_SET]


def test_remove_on_demand_opt_clears_cache() -> None:
    r = _FakeRedis()
    ensure_on_demand_opt(r, [CK_A, CK_B], now=1_000.0)
    cache_a = f"{IB_OPTION_CACHE_PREFIX}{CK_A}"
    r.set(cache_a, '{"last":1}')
    n = remove_on_demand_opt(r, [CK_A])
    assert n == 1
    assert CK_A not in r.sets[IB_OPTION_ON_DEMAND_SET]
    assert cache_a not in r.kv
    assert CK_B in r.sets[IB_OPTION_ON_DEMAND_SET]
