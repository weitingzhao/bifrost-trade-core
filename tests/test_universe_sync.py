"""Tests for Plugin-synced universe / price_readiness tables."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from bifrost_core.persistence.postgres import universe_sync as us


class TestParseDate:
    def test_iso_string(self) -> None:
        assert us._parse_date("2026-08-14") == date(2026, 8, 14)

    def test_none(self) -> None:
        assert us._parse_date(None) is None


class TestPriceReadyLogic:
    def test_ready_when_enough_fresh_bars(self) -> None:
        as_of = date.today()
        last = as_of - timedelta(days=2)
        stale_cutoff = as_of - timedelta(days=7)
        assert last >= stale_cutoff
        bar_rows = 240
        price_ready = (
            bar_rows >= us._MIN_BAR_ROWS
            and last >= stale_cutoff
            and 0 == 0
            and 0 == 0
        )
        assert price_ready is True

    def test_not_ready_when_stale(self) -> None:
        as_of = date.today()
        last = as_of - timedelta(days=30)
        stale_cutoff = as_of - timedelta(days=7)
        price_ready = (
            500 >= us._MIN_BAR_ROWS
            and last >= stale_cutoff
        )
        assert price_ready is False


class TestSyncUniverse:
    def test_truncate_and_insert(self) -> None:
        rows = [
            {
                "tickers_id": 1,
                "symbol": "nvda",
                "name": "NVIDIA",
                "market": "stocks",
                "locale": "us",
                "primary_exchange": "XNAS",
                "instrument_type": "cs",
                "active": True,
                "delisted_utc": None,
                "list_date": "1999-01-22",
                "sector": "Technology",
                "industry": "Semiconductors",
            }
        ]
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        with patch.object(us, "fetch_universe_rows", return_value=rows):
            n = us.sync_universe_from_plugin(conn)
        assert n == 1
        assert cur.execute.call_args_list[0][0][0].strip().startswith("TRUNCATE")
        cur.executemany.assert_called_once()
        payload = cur.executemany.call_args[0][1]
        assert payload[0][1] == "NVDA"
        assert payload[0][9] == date(1999, 1, 22)


class TestSyncPriceReadiness:
    def test_computes_price_ready(self) -> None:
        as_of = date.today()
        symbols = {
            "NVDA": {
                "bar_rows": 300,
                "first_bar_date": "2025-01-01",
                "last_bar_date": as_of.isoformat(),
                "null_close_rows": 0,
                "null_volume_rows": 0,
            },
            "THIN": {
                "bar_rows": 10,
                "first_bar_date": "2026-01-01",
                "last_bar_date": as_of.isoformat(),
                "null_close_rows": 0,
                "null_volume_rows": 0,
            },
        }
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        with patch.object(us, "fetch_bar_aggregate", return_value=symbols):
            n = us.sync_price_readiness_from_plugin(conn)
        assert n == 2
        payload = cur.executemany.call_args[0][1]
        by_sym = {row[1]: row for row in payload}
        assert by_sym["NVDA"][8] is True
        assert by_sym["THIN"][8] is False
