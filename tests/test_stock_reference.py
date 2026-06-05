"""Unit tests for ticker reference mappers (Massive/Polygon-shaped dicts)."""

from __future__ import annotations

from bifrost_core.persistence.postgres.ticker_reference import (
    count_ticker_overview_coverage,
    count_ticker_related_coverage,
    count_ticker_types_rows,
    count_tickers_rows,
    list_tickers_filled_related_page,
    list_tickers_missing_overview_page,
    list_tickers_missing_related_page,
    next_cursor_from_api_response,
    normalize_ticker_ref_kind,
    overview_stub_cols_api_not_found,
    row_from_ticker_detail,
    row_from_ticker_list_item,
    symbols_missing_overview_only,
    symbols_missing_related_only,
)


def test_row_from_ticker_list_item_maps_type_and_exchange():
    row = row_from_ticker_list_item(
        {
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "type": "CS",
            "active": True,
            "primary_exchange": "XNAS",
            "market": "stocks",
            "locale": "us",
            "cik": "0000320193",
        }
    )
    assert row["ticker"] == "AAPL"
    assert row["instrument_type"] == "CS"
    assert row["active"] is True
    assert row["primary_exchange"] == "XNAS"
    assert row["market"] == "stocks"


def test_row_from_ticker_list_item_currency_fields():
    row = row_from_ticker_list_item(
        {
            "ticker": "X",
            "name": "X",
            "currency_symbol": "USD",
            "base_currency_name": "US Dollar",
        }
    )
    assert row["currency_symbol"] == "USD"
    assert row["base_currency_name"] == "US Dollar"


def test_row_from_ticker_detail_branding_and_address():
    body = {
        "request_id": "req-1",
        "status": "OK",
        "count": 1,
        "results": {
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "primary_exchange": "XNAS",
            "market_cap": 3e12,
            "total_employees": 100000,
            "list_date": "1980-12-12",
            "ticker_root": "AAPL",
            "ticker_suffix": "",
            "sic_code": "3571",
            "sic_description": "ELECTRONIC COMPUTERS",
            "homepage_url": "https://www.apple.com",
            "round_lot": 100,
            "share_class_shares_outstanding": 16406400000,
            "weighted_shares_outstanding": 16334371000,
            "address": {
                "address1": "One Apple Park Way",
                "city": "Cupertino",
                "state": "CA",
                "postal_code": "95014",
            },
            "branding": {
                "icon_url": "https://example.com/icon.png",
                "logo_url": "https://example.com/logo.svg",
            },
            "phone_number": "+1-555-0100",
        },
    }
    tcols, dcols = row_from_ticker_detail(body)
    assert tcols["ticker"] == "AAPL"
    assert tcols["primary_exchange"] == "XNAS"
    assert dcols["exchange"] == "XNAS"
    assert dcols["address_city"] == "Cupertino"
    assert dcols["icon_url"] == "https://example.com/icon.png"
    assert dcols["market_cap"] == 3e12
    assert dcols["sic_code"] == "3571"
    assert dcols["homepage_url"] == "https://www.apple.com"
    assert dcols["round_lot"] == 100
    assert dcols["share_class_shares_outstanding"] == 16406400000.0
    assert dcols["weighted_shares_outstanding"] == 16334371000.0
    assert dcols["overview_api_request_id"] == "req-1"
    assert dcols["overview_api_status"] == "OK"
    assert dcols["overview_api_count"] == 1


def test_next_cursor_from_next_url():
    data = {"next_url": "https://api.example.com/v3/reference/tickers?cursor=abc123&limit=1000"}
    assert next_cursor_from_api_response(data) == "abc123"


def test_next_cursor_plain():
    assert next_cursor_from_api_response({"next_cursor": "xyz"}) == "xyz"


def test_normalize_ticker_ref_kind_maps_legacy_instrument_types():
    assert normalize_ticker_ref_kind("ticker_reference_instrument_types") == "feed_stocks_tickers_types"
    assert normalize_ticker_ref_kind("stock_reference_instrument_types") == "feed_stocks_tickers_types"
    assert normalize_ticker_ref_kind("ticker_reference_ticker_types") == "feed_stocks_tickers_types"
    assert normalize_ticker_ref_kind("feed_stocks_tickers_types") == "feed_stocks_tickers_types"


def test_normalize_ticker_ref_kind_maps_snapshot_to_feed_option_snapshots():
    assert normalize_ticker_ref_kind("snapshot") == "feed_option_snapshots"
    assert normalize_ticker_ref_kind("feed_option_snapshots") == "feed_option_snapshots"


def test_normalize_ticker_ref_kind_maps_stock_ohlc_sync_to_feed_stocks_aggregate():
    assert normalize_ticker_ref_kind("stock_ohlc_sync") == "feed_stocks_aggregate"
    assert normalize_ticker_ref_kind("feed_stocks_aggregate") == "feed_stocks_aggregate"


def test_normalize_ticker_ref_kind_maps_aggregates_to_feed_options_aggregate():
    assert normalize_ticker_ref_kind("aggregates") == "feed_options_aggregate"
    assert normalize_ticker_ref_kind("feed_options_aggregate") == "feed_options_aggregate"


def test_normalize_ticker_ref_kind_maps_related_to_feed_stocks_tickers_related():
    assert normalize_ticker_ref_kind("ticker_reference_related") == "feed_stocks_tickers_related"
    assert normalize_ticker_ref_kind("stock_reference_related") == "feed_stocks_tickers_related"
    assert normalize_ticker_ref_kind("feed_stocks_tickers_related") == "feed_stocks_tickers_related"


def test_normalize_ticker_ref_kind_maps_overview_to_feed_stocks_tickers_overview():
    assert normalize_ticker_ref_kind("ticker_reference_overview") == "feed_stocks_tickers_overview"
    assert normalize_ticker_ref_kind("stock_reference_overview") == "feed_stocks_tickers_overview"
    assert normalize_ticker_ref_kind("feed_stocks_tickers_overview") == "feed_stocks_tickers_overview"


def test_normalize_ticker_ref_kind_maps_trades_quotes_to_feed_options_trades_quotes():
    assert normalize_ticker_ref_kind("trades_quotes") == "feed_options_trades_quotes"
    assert normalize_ticker_ref_kind("feed_options_trades_quotes") == "feed_options_trades_quotes"


def test_normalize_ticker_ref_kind_maps_contracts_to_feed_option_contracts():
    assert normalize_ticker_ref_kind("contracts") == "feed_option_contracts"
    assert normalize_ticker_ref_kind("feed_option_contracts") == "feed_option_contracts"


def test_normalize_ticker_ref_kind_maps_universe_to_feed_stocks_tickers_reference_universe():
    assert normalize_ticker_ref_kind("stock_reference_universe") == "feed_stocks_tickers_reference_universe"
    assert normalize_ticker_ref_kind("ticker_reference_universe") == "feed_stocks_tickers_reference_universe"
    assert normalize_ticker_ref_kind("feed_stocks_tickers_reference_universe") == "feed_stocks_tickers_reference_universe"


def test_overview_stub_cols_api_not_found_sets_timestamp():
    stub = overview_stub_cols_api_not_found()
    assert stub["sector"] == ""
    assert stub["industry"] == ""
    assert stub["overview_updated_at"] is not None


def test_symbols_missing_overview_only_returns_tickers_without_overview_row():
    class _Cur:
        def execute(self, *_a, **_k):
            return None

        def fetchall(self):
            return [("AAA",), ("ZZZ",)]

    assert symbols_missing_overview_only(_Cur()) == ["AAA", "ZZZ"]


def test_count_ticker_overview_coverage_maps_row():
    class _Cur:
        def execute(self, *_a, **_k):
            return None

        def fetchone(self):
            return (10000, 9200, 800)

    assert count_ticker_overview_coverage(_Cur()) == {
        "total_tickers": 10000,
        "filled": 9200,
        "missing": 800,
    }


def test_list_tickers_missing_overview_page_respects_limit():
    class _Cur:
        def execute(self, sql, params):
            self.params = params

        def fetchall(self):
            return [("A",), ("B",)]

    cur = _Cur()
    assert list_tickers_missing_overview_page(cur, 2, 10) == ["A", "B"]
    assert cur.params == (2, 10)


def test_count_ticker_related_coverage_maps_row():
    class _Cur:
        def execute(self, *_a, **_k):
            return None

        def fetchone(self):
            return (5000, 800, 4200)

    assert count_ticker_related_coverage(_Cur()) == {
        "total_tickers": 5000,
        "filled": 800,
        "missing": 4200,
    }


def test_list_tickers_missing_related_page_params():
    class _Cur:
        def execute(self, _sql, params):
            self.params = params

        def fetchall(self):
            return [("Z",)]

    cur = _Cur()
    assert list_tickers_missing_related_page(cur, 3, 99) == ["Z"]
    assert cur.params == (3, 99)


def test_list_tickers_filled_related_page_params():
    class _Cur:
        def execute(self, _sql, params):
            self.params = params

        def fetchall(self):
            return [("A",), ("B",)]

    cur = _Cur()
    assert list_tickers_filled_related_page(cur, 2, 0) == ["A", "B"]
    assert cur.params == (2, 0)


def test_symbols_missing_related_only_returns_tickers_without_related_rows():
    class _Cur:
        def execute(self, *_a, **_k):
            return None

        def fetchall(self):
            return [("AAA",), ("ZZZ",)]

    assert symbols_missing_related_only(_Cur()) == ["AAA", "ZZZ"]


def test_count_tickers_rows():
    class _Cur:
        def execute(self, *_a, **_k):
            return None

        def fetchone(self):
            return (42_000,)

    assert count_tickers_rows(_Cur()) == 42000


def test_count_ticker_types_rows():
    class _Cur:
        def execute(self, *_a, **_k):
            return None

        def fetchone(self):
            return (128,)

    assert count_ticker_types_rows(_Cur()) == 128
