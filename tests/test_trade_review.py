"""trade_review without a database: tag cleaning and the row shape callers read."""

from __future__ import annotations

from datetime import datetime, timezone

from bifrost_core.monitor.reader.trade_review import TAG_MAX, _row_out, clean_tags


def test_clean_tags_trims_dedupes_and_bounds() -> None:
    assert clean_tags(["  roll early ", "roll early", "", None, "held to expiry"]) == [
        "roll early",
        "held to expiry",
    ]
    assert len(clean_tags([f"t{i}" for i in range(TAG_MAX + 5)])) == TAG_MAX


def test_clean_tags_keeps_none_as_keep_what_is_stored() -> None:
    assert clean_tags(None) is None
    assert clean_tags([]) == []


def test_row_out_decodes_json_and_says_whether_it_is_reviewed() -> None:
    row = _row_out(
        {
            "trade_review_id": 1,
            "strategy_instance_id": 7,
            "tags_added": '["late exit"]',
            "tags_dropped": ["held_to_expiry"],
            "reviewed_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        }
    )
    assert row["tags_added"] == ["late exit"]
    assert row["tags_dropped"] == ["held_to_expiry"]
    assert row["reviewed"] is True
    assert _row_out({"tags_added": None, "tags_dropped": None, "reviewed_at": None})["reviewed"] is False
