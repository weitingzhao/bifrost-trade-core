"""Anti-regression: readiness tables must not be recreated in Trade public.*."""

from __future__ import annotations

from pathlib import Path

DDL_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "bifrost_core"
    / "persistence"
    / "postgres"
    / "ddl.py"
)


def test_preference_data_gap_ack_is_dropped_not_created() -> None:
    text = DDL_PATH.read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS public.preference_data_gap_ack" in text
    assert "DROP TABLE IF EXISTS public.preference_sepa_gap_ack" in text
    # Must not CREATE the retired readiness ack table anymore.
    assert "CREATE TABLE IF NOT EXISTS preference_data_gap_ack" not in text
    assert "preference_data_gap_ack" in text  # still mentioned in drop / comments
    assert "_P8_RETIRED_PUBLIC_TABLES" in text
