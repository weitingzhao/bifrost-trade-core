"""Wave 10: strategy_dim_catalog literals must match PostgreSQL dim_*_t enum definitions."""

from typing import Any, List, Tuple

import pytest

from bifrost_core.monitor.reader.strategy_dim_catalog import (
    DIM_TYPE_TO_ENUM,
    dim_literals_by_type,
    is_valid_dim_code,
    validate_dim_fields,
)
from bifrost_core.persistence.postgres.seed_call_spread_templates import _CALL_SPREAD_TEMPLATE_SPECS
from bifrost_core.persistence.postgres.wave9_migrations import _DIM_TYPE_TO_ENUM, dim_enum_drift

# Labels of the dim_*_t types in bifrost_dev, bifrost_stg and bifrost_prod, read
# 2026-09-26. ensure_dim_enum_types never alters an existing type, so a catalog
# that leaves this set is refused by the databases (or refuses what they hold).
# Changing it means ALTER TYPE ... ADD VALUE in every env in the same change.
LIVE_ENUM_LABELS = {
    "direction": {"bullish", "bearish", "neutral"},
    "structure": {
        "single_leg",
        "vertical",
        "calendar",
        "diagonal",
        "straddle",
        "condor",
        "butterfly",
        "ratio",
        "custom",
    },
    "coverage": {"covered", "naked", "cash_secured", "synthetic"},
    "risk": {"defined", "undefined"},
    "volatility": {"long_vol", "short_vol", "vol_neutral"},
    "time": {"weekly", "monthly", "leaps", "flex"},
}


def test_dim_type_to_enum_matches_catalog():
    assert _DIM_TYPE_TO_ENUM == DIM_TYPE_TO_ENUM


def test_dim_catalog_literals_complete():
    literals = dim_literals_by_type()
    assert set(literals.keys()) == set(DIM_TYPE_TO_ENUM.keys())
    for dim_type, codes in literals.items():
        assert codes, f"dim_type {dim_type} must have at least one code"
        assert all(isinstance(c, str) and c for c in codes)


def test_catalog_is_the_live_enum_vocabulary():
    literals = {dim_type: set(codes) for dim_type, codes in dim_literals_by_type().items()}
    assert literals == LIVE_ENUM_LABELS


def test_call_spread_seed_only_prefers_catalog_codes():
    for spec in _CALL_SPREAD_TEMPLATE_SPECS:
        for dim_type, candidates in spec["dim_preferences"].items():
            for code in candidates:
                assert is_valid_dim_code(dim_type, code), f"{spec['template_code']}: {dim_type}={code}"


class _EnumCursor:
    def __init__(self, rows: List[Tuple[str, str]]) -> None:
        self._rows = rows
        self.params: Any = None

    def execute(self, sql: str, params: Any = None) -> None:
        self.params = params

    def fetchall(self) -> List[Tuple[str, str]]:
        return self._rows


def _rows(labels_by_type: dict) -> List[Tuple[str, str]]:
    return [
        (DIM_TYPE_TO_ENUM[dim_type], label)
        for dim_type, labels in labels_by_type.items()
        for label in labels
    ]


def test_dim_enum_drift_is_empty_when_types_match_the_catalog():
    assert dim_enum_drift(_EnumCursor(_rows(LIVE_ENUM_LABELS))) == {}


def test_dim_enum_drift_names_both_sides():
    labels = {**LIVE_ENUM_LABELS, "coverage": {"covered", "naked", "cash_secured", "uncovered"}}
    drift = dim_enum_drift(_EnumCursor(_rows(labels)))
    assert drift == {"dim_coverage_t": {"catalog_only": ["synthetic"], "db_only": ["uncovered"]}}


def test_dim_enum_drift_ignores_a_missing_type():
    labels = {k: v for k, v in LIVE_ENUM_LABELS.items() if k != "time"}
    assert dim_enum_drift(_EnumCursor(_rows(labels))) == {}


@pytest.mark.db
def test_ddl_built_types_agree_with_the_catalog(pg_conn):
    with pg_conn.cursor() as cur:
        assert dim_enum_drift(cur) == {}


def test_validate_dim_fields_allows_blank_and_known_codes():
    validate_dim_fields({"dim_coverage": "cash_secured", "dim_time": "", "dim_risk": None, "name": "x"})


def test_validate_dim_fields_names_the_refused_code():
    with pytest.raises(ValueError, match="Invalid coverage code: uncovered"):
        validate_dim_fields({"dim_coverage": " uncovered "})
