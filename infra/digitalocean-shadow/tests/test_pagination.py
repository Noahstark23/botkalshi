from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pagination", BASE / "pagination.py")
pagination = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pagination)


class FakeReader:
    def __init__(self, pages, books):
        self.pages = list(pages)
        self.books = books
        self.market_calls = 0
        self.book_calls = []

    def get_json(self, origin, path, params=None):
        if path == "/markets":
            self.market_calls += 1
            return self.pages.pop(0)
        ticker = path.split("/")[2]
        self.book_calls.append(ticker)
        return self.books[ticker]


def levels(book):
    return book if isinstance(book, dict) else {}


class PaginationTests(unittest.TestCase):
    NOW = datetime(2026, 9, 16, 16, 0, tzinfo=UTC)

    def test_reads_all_pages_and_deduplicates(self):
        r = FakeReader(
            [
                {"markets": [
                    {"ticker": "AAA-1", "close_time": "2026-09-16T18:00:00Z", "status": "open"},
                    {"ticker": "BBB-1", "close_time": "2026-09-17T18:00:00Z", "status": "open"},
                ], "cursor": "c1"},
                {"markets": [
                    {"ticker": "AAA-1", "close_time": "2026-09-16T18:00:00Z", "status": "open"},
                    {"ticker": "CCC-1", "close_time": "2026-09-18T18:00:00Z", "status": "open"},
                ], "cursor": ""},
            ],
            {"AAA-1": {"orderbook": {"yes": [[50, 1]]}}, "BBB-1": {"orderbook": {}}, "CCC-1": {"orderbook": {}}},
        )
        markets, coverage = pagination.collect_paginated(
            r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
            now=self.NOW, horizon_hours=72, page_size=2, max_pages=5, max_orderbooks=10,
        )
        self.assertEqual([m["ticker"] for m in markets], ["AAA-1", "BBB-1", "CCC-1"])
        self.assertEqual(coverage["pages_fetched"], 2)
        self.assertTrue(coverage["cursor_exhausted"])
        self.assertEqual(coverage["duplicate_tickers"], 1)
        self.assertFalse(coverage["truncated_by_page_limit"])

    def test_reports_orderbook_truncation(self):
        page = {"markets": [
            {"ticker": "AAA-1", "close_time": "2026-09-16T18:00:00Z", "status": "open"},
            {"ticker": "BBB-1", "close_time": "2026-09-16T19:00:00Z", "status": "open"},
        ], "cursor": ""}
        r = FakeReader([page], {"AAA-1": {"orderbook": {}}, "BBB-1": {"orderbook": {}}})
        markets, coverage = pagination.collect_paginated(
            r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
            now=self.NOW, max_orderbooks=1,
        )
        self.assertEqual(len(markets), 1)
        self.assertTrue(coverage["truncated_by_orderbook_limit"])
        self.assertEqual(coverage["eligible_in_horizon"], 2)

    def test_repeated_cursor_fails_closed(self):
        r = FakeReader(
            [
                {"markets": [], "cursor": "same"},
                {"markets": [], "cursor": "same"},
            ],
            {},
        )
        with self.assertRaises(pagination.PaginationError):
            pagination.collect_paginated(
                r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
                now=self.NOW,
            )

    def test_invalid_close_time_is_counted_not_invented(self):
        r = FakeReader(
            [{"markets": [{"ticker": "AAA-1", "close_time": "not-a-time", "status": "open"}], "cursor": ""}],
            {},
        )
        markets, coverage = pagination.collect_paginated(
            r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
            now=self.NOW,
        )
        self.assertEqual(markets, [])
        self.assertEqual(coverage["invalid_close_time"], 1)


if __name__ == "__main__":
    unittest.main()
