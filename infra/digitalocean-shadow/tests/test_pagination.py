from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys
import time
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
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

    def test_book_observed_at_is_recorded_per_book_from_its_own_fetch(self):
        r = FakeReader(
            [{"markets": [
                {"ticker": "AAA-1", "close_time": "2026-09-16T18:00:00Z", "status": "open"},
            ], "cursor": ""}],
            {"AAA-1": {"orderbook": {"yes": [[50, 1]]}}},
        )
        before = datetime.now(UTC)
        markets, coverage = pagination.collect_paginated(
            r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
            now=self.NOW, horizon_hours=72,
        )
        after = datetime.now(UTC)
        self.assertEqual(len(markets), 1)
        observed_raw = markets[0]["book_observed_at"]
        observed_at = datetime.fromisoformat(observed_raw)
        # Its own clock read at fetch time, not the cycle-start `now` used for
        # horizon filtering nor coverage.generated_at (both fixed at self.NOW).
        self.assertNotEqual(observed_raw, self.NOW.isoformat())
        self.assertNotEqual(observed_raw, coverage["generated_at"])
        self.assertTrue(before <= observed_at <= after)

    def test_cap_above_shared_contract_is_rejected_before_network(self):
        r = FakeReader([], {})
        with self.assertRaises(pagination.PaginationError):
            pagination.collect_paginated(
                r,
                origin="https://example.test",
                series="KXMLBGAME",
                sanitize_levels=levels,
                now=self.NOW,
                max_orderbooks=101,
            )
        self.assertEqual(r.market_calls, 0)

    def test_two_books_get_independent_timestamps_not_report_time(self):
        # Sleeping between the two orderbook fetches makes the difference in
        # book_observed_at deterministic instead of relying on clock
        # granularity racing the two datetime.now(UTC) calls.
        class SlowFakeReader(FakeReader):
            def get_json(self, origin, path, params=None):
                if path != "/markets" and self.book_calls:
                    time.sleep(0.02)
                return super().get_json(origin, path, params)

        r = SlowFakeReader(
            [{"markets": [
                {"ticker": "AAA-1", "close_time": "2026-09-16T18:00:00Z", "status": "open"},
                {"ticker": "BBB-1", "close_time": "2026-09-16T19:00:00Z", "status": "open"},
            ], "cursor": ""}],
            {"AAA-1": {"orderbook": {}}, "BBB-1": {"orderbook": {}}},
        )
        markets, coverage = pagination.collect_paginated(
            r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
            now=self.NOW,
        )
        self.assertEqual(len(markets), 2)
        first, second = markets[0]["book_observed_at"], markets[1]["book_observed_at"]
        self.assertNotEqual(first, second)
        self.assertLess(datetime.fromisoformat(first), datetime.fromisoformat(second))
        for observed in (first, second):
            self.assertNotEqual(observed, coverage["generated_at"])
            self.assertNotEqual(observed, self.NOW.isoformat())

    def test_m1_book_preserves_exact_strings_from_orderbook_fp(self):
        r = FakeReader(
            [{"markets": [
                {"ticker": "AAA-1", "close_time": "2026-09-16T18:00:00Z", "status": "open"},
            ], "cursor": ""}],
            {"AAA-1": {"orderbook_fp": {
                "yes_dollars": [["0.4321", "3.50"]],
                "no_dollars": [["0.0001", "12.34"]],
            }}},
        )
        markets, _ = pagination.collect_paginated(
            r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
            now=self.NOW,
        )
        self.assertEqual(
            markets[0]["m1_book"],
            {"yes_bids": [["0.4321", "3.50"]], "no_bids": [["0.0001", "12.34"]]},
        )

    def test_m1_book_fails_closed_on_malformed_row_mixed_with_valid_rows(self):
        r = FakeReader(
            [{"markets": [
                {"ticker": "AAA-1", "close_time": "2026-09-16T18:00:00Z", "status": "open"},
            ], "cursor": ""}],
            {"AAA-1": {"orderbook_fp": {
                "yes_dollars": [["0.4500", "10"], [0.30, "5"]],
                "no_dollars": [["0.4000", "7"]],
            }}},
        )
        markets, _ = pagination.collect_paginated(
            r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
            now=self.NOW,
        )
        self.assertIsNone(markets[0]["m1_book"])

    def test_m1_book_fails_closed_when_a_side_is_missing(self):
        r = FakeReader(
            [{"markets": [
                {"ticker": "AAA-1", "close_time": "2026-09-16T18:00:00Z", "status": "open"},
            ], "cursor": ""}],
            {"AAA-1": {"orderbook_fp": {"yes_dollars": [["0.4500", "10"]]}}},
        )
        markets, _ = pagination.collect_paginated(
            r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
            now=self.NOW,
        )
        self.assertIsNone(markets[0]["m1_book"])

    def test_m1_book_never_falls_back_to_integer_cent_orderbook(self):
        r = FakeReader(
            [{"markets": [
                {"ticker": "AAA-1", "close_time": "2026-09-16T18:00:00Z", "status": "open"},
            ], "cursor": ""}],
            {"AAA-1": {"orderbook": {"yes": [[50, 2]], "no": [[45, 3]]}}},
        )
        markets, _ = pagination.collect_paginated(
            r, origin="https://example.test", series="KXMLBGAME", sanitize_levels=levels,
            now=self.NOW,
        )
        self.assertIsNone(markets[0]["m1_book"])
        # General-purpose `levels` compatibility is untouched by the strict path.
        self.assertEqual(markets[0]["levels"], {"yes": [[50, 2]], "no": [[45, 3]]})


if __name__ == "__main__":
    unittest.main()
