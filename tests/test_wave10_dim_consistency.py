"""Wave 10: strategy_dim_catalog literals must match PostgreSQL dim_*_t enum definitions."""

from bifrost_core.monitor.reader.strategy_dim_catalog import (
    DIM_TYPE_TO_ENUM,
    dim_literals_by_type,
)
from bifrost_core.persistence.postgres.wave9_migrations import _DIM_TYPE_TO_ENUM


def test_dim_type_to_enum_matches_catalog():
    assert _DIM_TYPE_TO_ENUM == DIM_TYPE_TO_ENUM


def test_dim_catalog_literals_complete():
    literals = dim_literals_by_type()
    assert set(literals.keys()) == set(DIM_TYPE_TO_ENUM.keys())
    for dim_type, codes in literals.items():
        assert codes, f"dim_type {dim_type} must have at least one code"
        assert all(isinstance(c, str) and c for c in codes)
