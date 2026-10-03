"""One precedence rule for every connection setting: env, then YAML, then defaults (debt TD-54).

Before: the Postgres builders let YAML win while their docstrings said env won, the live
Redis bus let YAML win, and the IB bus let env win. Values here are invented.
"""

from __future__ import annotations

import pytest

from bifrost_core.core.redis_url import effective_ib_redis_dict, effective_redis_dict
from bifrost_core.persistence.postgres.connection import (
    _get_conn_params,
    _get_golden_source_conn_params,
    get_golden_source_conn_params,
)

_ENV = [
    "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD",
    "GOLDEN_SOURCE_HOST", "GOLDEN_SOURCE_PORT", "GOLDEN_SOURCE_DATABASE", "GOLDEN_SOURCE_USER",
    "GOLDEN_SOURCE_PASSWORD",
    "REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD", "REDIS_USERNAME",
    "REDIS_IB_HOST", "REDIS_IB_PORT", "REDIS_IB_DB", "REDIS_IB_PASSWORD", "REDIS_IB_USERNAME",
]

CONFIG = {
    "postgres": {"host": "pg.yaml", "port": 5432, "database": "bifrost_dev", "user": "bifrost", "password": ""},
    "golden_source": {"host": "gs.yaml", "database": "bifrost_golden_source", "user": "bifrost", "password": ""},
    "redis": {"host": "redis.yaml", "port": 6379, "db": 0},
    "redis_ib": {"host": "redis-ib.yaml", "port": 6379, "db": 0, "username": "trade-dev", "password": ""},
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def test_yaml_when_no_env() -> None:
    assert _get_conn_params(CONFIG)["host"] == "pg.yaml"
    gs = _get_golden_source_conn_params(CONFIG)
    assert (gs["host"], gs["dbname"], gs["user"]) == ("gs.yaml", "bifrost_golden_source", "bifrost")


def test_env_beats_yaml_for_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "pg.env")
    monkeypatch.setenv("PGPASSWORD", "pw-env")
    p = _get_conn_params(CONFIG)
    assert (p["host"], p["password"], p["dbname"]) == ("pg.env", "pw-env", "bifrost_dev")


def test_golden_source_user_takes_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """It used to lose to the overlay's `user: bifrost`."""
    monkeypatch.setenv("GOLDEN_SOURCE_USER", "gs-env-user")
    assert get_golden_source_conn_params(CONFIG)["user"] == "gs-env-user"


def test_golden_source_falls_back_to_the_trade_database_but_never_its_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "pg.env")
    monkeypatch.setenv("PGPASSWORD", "trade-pw")
    cfg = {"postgres": CONFIG["postgres"], "golden_source": {}}
    gs = _get_golden_source_conn_params(cfg)
    assert (gs["host"], gs["password"], gs["dbname"]) == ("pg.env", "trade-pw", "bifrost_golden_source")
    monkeypatch.setenv("GOLDEN_SOURCE_PASSWORD", "gs-pw")
    assert _get_golden_source_conn_params(cfg)["password"] == "gs-pw"


def test_env_beats_yaml_for_the_live_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    assert effective_redis_dict(CONFIG)["host"] == "redis.yaml"
    monkeypatch.setenv("REDIS_HOST", "redis.env")
    monkeypatch.setenv("REDIS_DB", "3")
    r = effective_redis_dict(CONFIG)
    assert (r["host"], r["db"]) == ("redis.env", 3)


def test_ib_bus_is_resolved_per_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_HOST", "redis.env")  # meant for the live bus only
    monkeypatch.setenv("REDIS_IB_PASSWORD", "ib-pw")
    ib = effective_ib_redis_dict(CONFIG)
    assert ib["host"] == "redis-ib.yaml"
    assert (ib["username"], ib["password"]) == ("trade-dev", "ib-pw")
    monkeypatch.setenv("REDIS_IB_USERNAME", "trade-stg")
    assert effective_ib_redis_dict(CONFIG)["username"] == "trade-stg"


def test_no_ib_host_means_the_live_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = {"redis": CONFIG["redis"]}
    assert effective_ib_redis_dict(cfg) == effective_redis_dict(cfg)
    monkeypatch.setenv("REDIS_IB_HOST", "redis-ib.env")
    assert effective_ib_redis_dict(cfg)["host"] == "redis-ib.env"
