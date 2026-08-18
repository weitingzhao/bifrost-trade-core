"""Unit: per-env ddl.py must not recreate brokerage Golden Source objects."""

from __future__ import annotations

import re
from pathlib import Path

from bifrost_core.persistence.postgres.ddl import (
    _BROKERAGE_MIGRATED_TABLES,
    _BROKERAGE_MIGRATED_VIEWS,
)

DDL_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "bifrost_core" / "persistence" / "postgres" / "ddl.py"
)


def test_ddl_does_not_create_migrated_brokerage_tables() -> None:
    src = DDL_PATH.read_text(encoding="utf-8")
    for name in _BROKERAGE_MIGRATED_TABLES:
        pattern = rf"CREATE TABLE IF NOT EXISTS {re.escape(name)}\s*\("
        assert re.search(pattern, src) is None, name
    for name in _BROKERAGE_MIGRATED_VIEWS:
        pattern = rf"CREATE OR REPLACE VIEW {re.escape(name)}\s+AS"
        assert re.search(pattern, src, re.IGNORECASE) is None, name
