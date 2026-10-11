"""History capture tests with fake responses; all sockets fail closed."""

import base64
import copy
import hashlib
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import us500_history as history
from us500_engine import DataError, MAX_BYTES, save_new

START = 1791744840000
END = START + 120000
TICKER = "KXUS500PERP"


def iso(ms):
    return (
        (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms))
        .isoformat()
        .replace("+00:00", "Z")
    )


def trade(identifier="a", at=START, ticker=TICKER, price="13.5", count="2.00"):
    return {
        "trade_id": identifier,
        "ticker": ticker,
        "price": price,
        "count": count,
        "created_time": iso(at),
        "taker_side": "bid",
    }


def candle(at=START, close="13.5"):
    ohlc = {"open": "13.5", "high": "13.6", "low": "13.4", "close": close}
    return {
        "end_period_ts": at // 1000,
        "price": ohlc,
        "bid": None,
        "ask": None,
        "volume": "4",
        "open_interest": "10",
    }


def funding(at=START, rate=0, ticker=TICKER):
    return {
        "market_ticker": ticker,
        "funding_time": iso(at),
        "funding_rate": rate,
        "mark_price": "13.5",
    }


class Response:
    def __init__(self, body=None, *, raw=None, status=200):
        self.status = status
        self.raw = raw if raw is not None else json.dumps(body).encode()
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, count):
        self.read_sizes.append(count)
        return self.raw[:count]


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError("UNPLANNED_REQUEST")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class HistoryTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN"))
        guard.start()
        self.addCleanup(guard.stop)

    def capture(
        self,
        bodies,
        resource="trades",
        environment="public",
        maximum=3,
        start=START,
        end=END,
    ):
        responses = [
            body if isinstance(body, (Response, Exception)) else Response(body)
            for body in bodies
        ]
        transport = Transport(responses)
        reader = history.PublicHistoryReader(environment, maximum, transport)
        return reader.capture(resource, start, end), transport

    def test_trades_paginate_sort_deduplicate_and_record_exact_original_bytes(self):
        first = {
            "trades": [trade("later", START + 60000), trade("first")],
            "cursor": "next/value?opaque=1",
        }
        second = {"trades": [trade("first"), trade("last", END)], "cursor": ""}
        result, transport = self.capture([first, second])
        report = result["analysis"]
        self.assertTrue(report["valid_evidence"])
        self.assertTrue(report["retrieval_complete"])
        self.assertFalse(report["history_sufficient"])
        self.assertEqual(
            [r["id"] for r in report["records"]], ["first", "later", "last"]
        )
        self.assertEqual(report["identical_duplicates"], 1)
        self.assertEqual(report["observed_range_ms"], {"start": START, "end": END})
        raw = base64.b64decode(result["pages"][0]["raw_base64"])
        self.assertEqual(json.loads(raw), first)
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), result["pages"][0]["raw_sha256"]
        )
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(transport.calls[1][0].full_url).query
        )
        self.assertEqual(query["cursor"], ["next/value?opaque=1"])

    def test_only_expected_anonymous_gets_and_second_conversion_are_sent(self):
        result, transport = self.capture(
            [{"trades": [], "cursor": ""}], start=START + 123, end=END + 789
        )
        request, timeout = transport.calls[0]
        parsed = urllib.parse.urlsplit(request.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "external-api.kalshi.com")
        self.assertEqual(parsed.path, "/trade-api/v2/margin/trades")
        self.assertEqual(query["ticker"], [TICKER])
        self.assertEqual(query["min_ts"], [str((START + 123) // 1000 - 1)])
        self.assertEqual(query["max_ts"], [str((END + 789 + 999) // 1000 + 1)])
        self.assertEqual(query["limit"], ["1000"])
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(dict(request.header_items()), {"Accept": "application/json"})
        self.assertEqual(timeout, 5)
        self.assertFalse(result["authenticated"])

    def test_demo_ticker_and_host_are_distinct(self):
        result, transport = self.capture(
            [{"trades": [trade(ticker="KXUS500PERP1")], "cursor": ""}],
            environment="demo",
        )
        self.assertTrue(result["analysis"]["retrieval_complete"])
        self.assertEqual(result["ticker"], "KXUS500PERP1")
        self.assertTrue(
            transport.calls[0][0].full_url.startswith(
                "https://external-api.demo.kalshi.co/"
            )
        )

    def test_padded_response_records_are_filtered_against_exact_millisecond_range(self):
        rows = [
            trade("before", START - 1),
            trade("start", START),
            trade("end", END),
            trade("after", END + 1),
        ]
        result, _ = self.capture([{"trades": rows, "cursor": ""}])
        self.assertEqual(
            [r["id"] for r in result["analysis"]["records"]], ["start", "end"]
        )
        self.assertEqual(result["analysis"]["excluded_outside_window"], 2)

    def test_budget_exhaustion_keeps_partial_pages_and_is_not_complete(self):
        result, transport = self.capture(
            [{"trades": [trade()], "cursor": "more"}], maximum=1
        )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(
            result["capture_stop_reason"], "HISTORY_REQUEST_BUDGET_EXHAUSTED"
        )
        self.assertFalse(result["analysis"]["retrieval_complete"])
        self.assertEqual(result["analysis"]["record_count"], 1)

    def test_repeated_cursor_stops_without_a_third_request(self):
        result, transport = self.capture(
            [
                {"trades": [trade()], "cursor": "repeat"},
                {"trades": [trade("b", START + 1)], "cursor": "repeat"},
            ]
        )
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(result["capture_stop_reason"], "REPEATED_HISTORY_CURSOR")
        self.assertFalse(result["analysis"]["valid_evidence"])
        self.assertFalse(result["analysis"]["retrieval_complete"])

    def test_conflicting_trade_id_retains_evidence_and_stops(self):
        result, transport = self.capture(
            [
                {"trades": [trade()], "cursor": "second"},
                {"trades": [trade(price="13.6")], "cursor": "third"},
            ]
        )
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual(result["capture_stop_reason"], "CONFLICTING_HISTORY_RECORD")
        self.assertFalse(result["analysis"]["retrieval_complete"])

    def test_empty_page_with_cursor_is_incomplete_and_never_spins(self):
        result, transport = self.capture([{"trades": [], "cursor": "more"}])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(
            result["capture_stop_reason"], "EMPTY_HISTORY_PAGE_WITH_CURSOR"
        )
        self.assertFalse(result["analysis"]["retrieval_complete"])

    def test_empty_successful_history_does_not_claim_sufficiency(self):
        result, _ = self.capture([{"trades": [], "cursor": ""}])
        report = result["analysis"]
        self.assertTrue(report["retrieval_complete"])
        self.assertFalse(report["history_sufficient"])
        self.assertEqual(report["coverage_state"], "EMPTY")
        self.assertIsNone(report["observed_range_ms"])

    def test_network_failure_consumes_one_attempt_with_no_retry(self):
        result, transport = self.capture(
            [urllib.error.URLError("potential private diagnostic")]
        )
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["requests_made"], 1)
        self.assertEqual(result["capture_stop_reason"], "HISTORY_NETWORK_UNAVAILABLE")
        self.assertNotIn("private", json.dumps(result, default=str))
        self.assertFalse(result["analysis"]["retrieval_complete"])

    def test_http_errors_do_not_leak_response_or_retry(self):
        error = urllib.error.HTTPError(
            "https://example.com?secret=never", 429, "private", {}, None
        )
        result, transport = self.capture([error])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["capture_stop_reason"], "HISTORY_HTTP_429")
        self.assertNotIn("secret", json.dumps(result))

    def test_bad_duplicate_nonfinite_and_oversized_json_are_incomplete(self):
        for raw in (
            b'{"trades":[],"trades":[]}',
            b'{"value":NaN}',
            b"[]",
            b" " * (MAX_BYTES + 1),
        ):
            response = Response(raw=raw)
            with self.subTest(size=len(raw)):
                result, transport = self.capture([response])
                self.assertFalse(result["analysis"]["retrieval_complete"])
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(response.read_sizes, [MAX_BYTES + 1])

    def test_wrong_ticker_and_float_money_fail_closed(self):
        for row in (
            trade(ticker="KXBTCPERP"),
            trade(price=13.5),
            trade(count="0"),
            trade(price="922337203685477.5807"),
        ):
            with self.subTest(row=row):
                result, _ = self.capture([{"trades": [row], "cursor": ""}])
                self.assertFalse(result["analysis"]["retrieval_complete"])

    def test_candles_report_missing_minute_without_imputing_price(self):
        result, transport = self.capture(
            [{"ticker": TICKER, "candlesticks": [candle(START), candle(END)]}],
            resource="candles",
        )
        report = result["analysis"]
        self.assertTrue(report["retrieval_complete"])
        self.assertEqual(report["cadence"]["expected_minute_boundaries"], 3)
        self.assertEqual(report["cadence"]["missing_candles"], 1)
        self.assertEqual(
            report["cadence"]["gap_ranges"],
            [
                {
                    "start_ms": START + 60000,
                    "end_ms": START + 60000,
                    "missing_candles": 1,
                }
            ],
        )
        self.assertFalse(report["cadence"]["calendar_verified"])
        query = urllib.parse.parse_qs(
            urllib.parse.urlsplit(transport.calls[0][0].full_url).query
        )
        self.assertEqual(query["period_interval"], ["1"])
        self.assertEqual(query["include_latest_before_start"], ["false"])

    def test_candles_missing_trade_prices_remain_null(self):
        row = candle()
        row["price"] = {"open": None, "high": None, "low": None, "close": None}
        row["volume"] = "0"
        result, _ = self.capture(
            [{"ticker": TICKER, "candlesticks": [row]}], resource="candles"
        )
        self.assertTrue(result["analysis"]["retrieval_complete"])
        self.assertEqual(result["analysis"]["candles_without_trade_prices"], 1)
        self.assertIsNone(result["analysis"]["records"][0]["price"]["close"])

    def test_inconsistent_partial_and_unaligned_candles_are_rejected(self):
        wrong_ohlc = candle(close="14")
        partial = candle()
        partial["price"]["close"] = None
        unaligned = candle()
        unaligned["end_period_ts"] += 1
        for row in (wrong_ohlc, partial, unaligned):
            with self.subTest(row=row):
                result, _ = self.capture(
                    [{"ticker": TICKER, "candlesticks": [row]}], resource="candles"
                )
                self.assertFalse(result["analysis"]["retrieval_complete"])

    def test_funding_numeric_json_is_decimal_and_signed(self):
        raw = (
            '{"funding_rates":[{"market_ticker":"KXUS500PERP","funding_time":"'
            + iso(START)
            + '","funding_rate":-0.00001234567890123456789,"mark_price":"13.5"}]}'
        ).encode()
        result, _ = self.capture([Response(raw=raw)], resource="funding")
        report = result["analysis"]
        self.assertTrue(report["retrieval_complete"])
        self.assertEqual(
            report["records"][0]["funding_rate"], Decimal("-0.00001234567890123456789")
        )
        self.assertIsNone(report["cadence"]["gap_count"])

    def test_funding_and_candles_split_day_windows_with_shared_budget(self):
        second_start = START + history.DAY_MS
        result, transport = self.capture(
            [
                {"funding_rates": [funding()]},
                {"funding_rates": [funding(second_start)]},
            ],
            resource="funding",
            end=second_start + 1000,
            maximum=2,
        )
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(result["analysis"]["retrieval_complete"])
        self.assertEqual(result["analysis"]["completed_windows"], 2)
        self.assertEqual(result["pages"][0]["window_end_ms"], second_start - 1)
        self.assertEqual(result["pages"][1]["window_start_ms"], second_start)

    def test_insufficient_window_budget_marks_explicit_partial_coverage(self):
        result, transport = self.capture(
            [{"funding_rates": [funding()]}],
            resource="funding",
            end=START + 2 * history.DAY_MS,
            maximum=1,
        )
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(result["analysis"]["retrieval_complete"])
        self.assertEqual(result["analysis"]["completed_windows"], 1)
        self.assertEqual(result["analysis"]["requested_windows"], 3)

    def test_funding_conflicts_are_not_averaged_or_silently_deduplicated(self):
        result, _ = self.capture(
            [{"funding_rates": [funding(rate=0), funding(rate="0.01")]}],
            resource="funding",
        )
        self.assertEqual(result["capture_stop_reason"], "CONFLICTING_HISTORY_RECORD")
        self.assertFalse(result["analysis"]["retrieval_complete"])

    def test_unsupported_server_pagination_for_nonpaginated_resource_is_incomplete(
        self,
    ):
        result, _ = self.capture(
            [{"funding_rates": [funding()], "cursor": "unexpected"}], resource="funding"
        )
        self.assertEqual(result["capture_stop_reason"], "UNEXPECTED_HISTORY_PAGINATION")

    def test_offline_verification_detects_raw_body_hash_and_url_tampering(self):
        original, _ = self.capture([{"trades": [trade()], "cursor": ""}])
        changes = (
            ("raw_sha256", "0" * 64),
            ("canonical_body_sha256", "0" * 64),
            ("raw_base64", base64.b64encode(b"{}").decode()),
            ("url", "https://example.com/"),
        )
        for key, value in changes:
            data = copy.deepcopy(original)
            data["pages"][0][key] = value
            with self.subTest(key=key):
                self.assertFalse(history.analyze_history(data)["valid_evidence"])
        data = copy.deepcopy(original)
        data["pages"][0]["body"]["trades"][0]["price"] = "99"
        self.assertFalse(history.analyze_history(data)["valid_evidence"])

    def test_offline_analysis_ignores_precomputed_claim_of_sufficiency(self):
        original, _ = self.capture([{"trades": [], "cursor": ""}])
        original["analysis"] = {"history_sufficient": True, "record_count": 999}
        result = history.analyze_history(original)
        self.assertFalse(result["history_sufficient"])
        self.assertEqual(result["record_count"], 0)
        self.assertFalse(result["source_authenticity_verified"])

    def test_unexplained_missing_response_cannot_claim_retrieval_complete(self):
        original, _ = self.capture([{"trades": [], "cursor": ""}])
        original["requests_made"] = 2
        result = history.analyze_history(original)
        self.assertFalse(result["valid_evidence"])
        self.assertIn("UNACCOUNTED_HISTORY_REQUEST", result["errors"])

    def test_internal_reader_also_rejects_arbitrary_resource_before_network(self):
        transport = Transport([])
        reader = history.PublicHistoryReader(transport=transport)
        with self.assertRaises(DataError):
            reader._get("https://example.com/orders", START, END, "")
        self.assertEqual(transport.calls, [])

    def test_saved_dataset_roundtrip_retains_exact_funding_and_verification(self):
        result, _ = self.capture(
            [{"funding_rates": [funding(rate="-0.000001")]}], resource="funding"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            save_new(path, result)
            loaded = history.load_history(path)
            verified = history.analyze_history(loaded)
        self.assertTrue(verified["valid_evidence"])
        self.assertEqual(verified["records"][0]["funding_rate"], Decimal("-0.000001"))

    def test_malformed_dataset_never_raises_unhandled_schema_exception(self):
        for data in (
            None,
            [],
            {},
            {"schema": history.SCHEMA},
            {"schema": history.SCHEMA, "environment": []},
        ):
            with self.subTest(data=data):
                result = history.analyze_history(data)
                self.assertFalse(result["valid_evidence"])
                self.assertFalse(result["retrieval_complete"])

    def test_resources_ranges_and_budgets_are_bounded_before_transport(self):
        transport = Transport([])
        for maximum in (0, -1, 21, True, 1.0):
            with self.subTest(maximum=maximum), self.assertRaises(DataError):
                history.PublicHistoryReader(max_pages=maximum, transport=transport)
        for environment in ("production", "https://example.com"):
            with self.subTest(environment=environment), self.assertRaises(DataError):
                history.PublicHistoryReader(environment, transport=transport)
        for resource in (
            "orders",
            "positions",
            "../../../accounts",
            "https://example.com",
        ):
            with self.subTest(resource=resource), self.assertRaises(DataError):
                history.PublicHistoryReader(transport=transport).capture(
                    resource, START, END
                )
        for start, end in (
            (END, START),
            (True, END),
            (START // 1000, END),
            (START, START + 367 * history.DAY_MS),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(DataError):
                history.PublicHistoryReader(transport=transport).capture(
                    "trades", start, end
                )
        self.assertEqual(transport.calls, [])

    def test_reader_cannot_reset_budget_by_starting_second_capture(self):
        transport = Transport([Response({"trades": [], "cursor": ""})])
        reader = history.PublicHistoryReader(max_pages=1, transport=transport)
        reader.capture("trades", START, END)
        with self.assertRaises(DataError):
            reader.capture("funding", START, END)
        self.assertEqual(len(transport.calls), 1)

    def test_default_transport_explicitly_disables_environment_proxies_and_redirects(
        self,
    ):
        with patch.object(history.urllib.request, "build_opener") as build:
            history.PublicHistoryReader()
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        with self.assertRaises(DataError):
            handlers[1].redirect_request(None, None, 302, "", {}, "https://example.com")

    def test_cli_existing_output_is_preserved_without_network_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.json"
            path.write_bytes(b"unchanged")
            args = [
                "us500_history.py",
                "capture-history",
                "--resource",
                "trades",
                "--start-ms",
                str(START),
                "--end-ms",
                str(END),
                "--output",
                str(path),
            ]
            with (
                patch("sys.argv", args),
                patch.object(history, "PublicHistoryReader") as reader,
                patch("builtins.print"),
            ):
                self.assertEqual(history.main(), 2)
                reader.assert_not_called()
            self.assertEqual(path.read_bytes(), b"unchanged")


if __name__ == "__main__":
    unittest.main()
