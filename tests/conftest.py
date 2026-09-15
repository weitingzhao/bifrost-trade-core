"""Shared pytest fixtures for bifrost-core."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def sample_config():
    return {
        "gates": {
            "state": {
                "delta": {
                    "threshold_hedge_shares": 25,
                    "epsilon_band": 10,
                    "max_delta_limit": 500,
                }
            }
        },
        "greeks": {
            "risk_free_rate": 0.05,
            "volatility": 0.35,
        },
    }


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def sample_yaml(project_root: Path) -> Path:
    return project_root / "config" / "config.yaml.example"


@pytest.fixture
def pg_conn(project_root: Path):
    """A PostgreSQL connection with the schema ensured, or skip.

    Shared: the DDL tests and the strategy_plan state machine both need a real
    database with `_ensure_tables` already run.
    """
    if not os.environ.get("PGHOST") and not os.environ.get("BIFROST_TEST_DB"):
        pytest.skip("Set PGHOST or BIFROST_TEST_DB=1 for db tests")
    import psycopg2
    import yaml

    from bifrost_core.persistence.postgres.connection import _get_conn_params
    from bifrost_core.persistence.postgres.ddl import _ensure_tables

    with open(project_root / "config" / "config.yaml.example", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    conn = psycopg2.connect(**_get_conn_params(config))
    _ensure_tables(conn)
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()
