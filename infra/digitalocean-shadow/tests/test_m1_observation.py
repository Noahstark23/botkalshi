"""Unit tests for m1_observation.review_m1_book. Pure stdlib, no network."""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import m1_observation as m1obs  # noqa: E402

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
FRESH_OBSERVED_AT = "2026-09-21T11:59:30+00:00"


def _payload(**overrides):
    base = {
        "schema_version": m1obs.SCHEMA_VERSION_INPUT,
        "cycle_id": "cycle-001",
        "ticker": "KXTEST-26",
        "book_observed_at": FRESH_OBSERVED_AT,
        "price_unit": "USD",
        "yes_bids": [["0.30", "5"], ["0.45", "10"], ["0.10", "2"]],
        "no_bids": [["0.15", "3"], ["0.40", "7"], ["0.05", "1"]],
    }
    base.update(overrides)
    return base


def _assert_flags_are_safe(case, result):
    case.assertFalse(result["execution_authorized"])
    case.assertFalse(result["order_capability_present"])
    case.assertFalse(result["real_entry_eligible"])
    case.assertEqual(result["recommendations"], [])
    case.assertEqual(result["real_orders"], [])
    case.assertIsNone(result["profit_realized_usd"])
    case.assertEqual(result["origin"], "M1")
    case.assertEqual(result["mode"], "SIMULATION_ONLY")
    case.assertEqual(result["evidence_state"], "DECLARED_BOOK_NOT_EXCHANGE_VERIFIED")
    case.assertEqual(result["freshness_window_seconds"], 180)


class HealthyBookTests(unittest.TestCase):
    def test_unordered_book_produces_exact_complementary_asks_and_no_arbitrage(self):
        result = m1obs.review_m1_book(_payload(), now=NOW)
        self.assertEqual(result["status"], "NO_ARBITRAGE")
        self.assertEqual(result["reason_codes"], [])
        self.assertTrue(result["legacy_detector_called"])
        self.assertEqual(result["best_yes_bid"], {"price_usd": "0.45", "quantity_contracts": "10"})
        self.assertEqual(result["best_no_bid"], {"price_usd": "0.40", "quantity_contracts": "7"})
        self.assertEqual(result["yes_ask"], {"price_usd": "0.6000", "quantity_contracts": "7"})
        self.assertEqual(result["no_ask"], {"price_usd": "0.5500", "quantity_contracts": "10"})
        _assert_flags_are_safe(self, result)

    def test_locked_book_does_not_produce_profit(self):
        payload = _payload(
            yes_bids=[["0.50", "5"]],
            no_bids=[["0.50", "5"]],
        )
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "NO_ARBITRAGE")
        self.assertTrue(result["legacy_detector_called"])
        self.assertIsNone(result["profit_realized_usd"])


class CrossedBookTests(unittest.TestCase):
    def test_crossed_book_blocks_without_invoking_detector(self):
        payload = _payload(
            yes_bids=[["0.60", "4"]],
            no_bids=[["0.55", "4"]],
        )
        with patch.object(m1obs, "detect_binary_arb") as mocked:
            result = m1obs.review_m1_book(payload, now=NOW)
        mocked.assert_not_called()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["CROSSED_OR_INCOHERENT_BOOK"])
        self.assertFalse(result["legacy_detector_called"])
        _assert_flags_are_safe(self, result)


class EmptySideTests(unittest.TestCase):
    def test_empty_yes_side_is_no_data(self):
        result = m1obs.review_m1_book(_payload(yes_bids=[]), now=NOW)
        self.assertEqual(result["status"], "NO_DATA")
        self.assertNotEqual(result["status"], "NO_ARBITRAGE")

    def test_empty_no_side_is_no_data(self):
        result = m1obs.review_m1_book(_payload(no_bids=[]), now=NOW)
        self.assertEqual(result["status"], "NO_DATA")


class MalformedRowTests(unittest.TestCase):
    def test_duplicate_price_level_blocks(self):
        payload = _payload(yes_bids=[["0.10", "1"], ["0.10", "2"]])
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("DUPLICATE_YES_PRICE_LEVEL", result["reason_codes"])

    def test_wrong_row_length_blocks(self):
        payload = _payload(no_bids=[["0.10"]])
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_NO_BID_ROW", result["reason_codes"])

    def test_row_not_a_pair_blocks(self):
        payload = _payload(yes_bids=[{"price": "0.10", "qty": "1"}])
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_YES_BID_ROW", result["reason_codes"])

    def test_tuple_row_blocks_closed_list_contract(self):
        payload = _payload(yes_bids=[("0.10", "1")])
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_YES_BID_ROW", result["reason_codes"])

    def test_unexpected_top_level_field_blocks_without_echoing_payload(self):
        payload = _payload(generated_at="untrusted")
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["UNEXPECTED_INPUT_FIELDS"])
        self.assertNotIn("generated_at", result)
        self.assertNotIn("untrusted", repr(result))

    def test_too_many_levels_blocks(self):
        rows = [[f"0.{i:04d}"[:6], "1"] for i in range(101)]
        payload = _payload(yes_bids=rows)
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("TOO_MANY_YES_LEVELS", result["reason_codes"])


class InvalidPriceUnitTests(unittest.TestCase):
    def test_non_usd_currency_blocks(self):
        result = m1obs.review_m1_book(_payload(price_unit="EUR"), now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_PRICE_UNIT", result["reason_codes"])


class NullAndShapeTests(unittest.TestCase):
    def test_raw_none_blocks(self):
        result = m1obs.review_m1_book(None, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_INPUT_SHAPE", result["reason_codes"])

    def test_null_fields_block_without_crashing(self):
        payload = _payload(cycle_id=None, ticker=None, book_observed_at=None)
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_CYCLE_ID", result["reason_codes"])
        self.assertIn("INVALID_TICKER", result["reason_codes"])
        self.assertIn("INVALID_BOOK_OBSERVED_AT", result["reason_codes"])
        self.assertIsNone(result["cycle_id"])
        self.assertIsNone(result["ticker"])


class FloatExponentOversizeTests(unittest.TestCase):
    def test_float_price_blocks(self):
        payload = _payload(yes_bids=[[0.30, "5"]])
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_YES_BID_ROW", result["reason_codes"])

    def test_bool_quantity_blocks(self):
        payload = _payload(yes_bids=[["0.30", True]])
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_YES_BID_ROW", result["reason_codes"])

    def test_exponent_string_price_blocks(self):
        payload = _payload(yes_bids=[["1e-1", "5"]])
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_YES_BID_ROW", result["reason_codes"])

    def test_nan_and_inf_strings_block(self):
        for bad in ("nan", "inf", "-inf", "NaN"):
            payload = _payload(yes_bids=[[bad, "5"]])
            result = m1obs.review_m1_book(payload, now=NOW)
            self.assertEqual(result["status"], "BLOCKED")
            self.assertIn("INVALID_YES_BID_ROW", result["reason_codes"])

    def test_oversize_price_string_blocks(self):
        payload = _payload(yes_bids=[["0." + "1" * 20, "5"]])
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_YES_BID_ROW", result["reason_codes"])

    def test_oversize_cycle_id_blocks(self):
        payload = _payload(cycle_id="c" * 200)
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_CYCLE_ID", result["reason_codes"])


class TimestampTests(unittest.TestCase):
    def test_future_book_blocks(self):
        future = (NOW + timedelta(seconds=10)).isoformat()
        result = m1obs.review_m1_book(_payload(book_observed_at=future), now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("BOOK_OBSERVED_IN_FUTURE", result["reason_codes"])

    def test_stale_book_blocks(self):
        stale = (NOW - timedelta(seconds=181)).isoformat()
        result = m1obs.review_m1_book(_payload(book_observed_at=stale), now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("BOOK_OBSERVATION_STALE", result["reason_codes"])

    def test_naive_timestamp_blocks(self):
        payload = _payload(book_observed_at="2026-09-21T11:59:30")
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_BOOK_OBSERVED_AT", result["reason_codes"])

    def test_overflowing_timestamp_blocks(self):
        payload = _payload(book_observed_at="9999-12-31T23:59:59-01:00")
        result = m1obs.review_m1_book(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_BOOK_OBSERVED_AT", result["reason_codes"])


class LegacyPrecisionBoundaryTests(unittest.TestCase):
    def test_fractional_price_returns_not_evaluated_without_flooring(self):
        payload = _payload(
            yes_bids=[["0.4567", "3"]],
            no_bids=[["0.10", "2"]],
        )
        with patch.object(m1obs, "detect_binary_arb") as mocked:
            result = m1obs.review_m1_book(payload, now=NOW)
        mocked.assert_not_called()
        self.assertEqual(result["status"], "NOT_EVALUATED")
        self.assertIn("UNSUPPORTED_LEGACY_PRECISION", result["reason_codes"])
        self.assertFalse(result["legacy_detector_called"])
        self.assertEqual(result["no_ask"], {"price_usd": "0.5433", "quantity_contracts": "3"})

    def test_fractional_quantity_returns_not_evaluated_without_flooring(self):
        payload = _payload(
            yes_bids=[["0.40", "2.5"]],
            no_bids=[["0.30", "4"]],
        )
        with patch.object(m1obs, "detect_binary_arb") as mocked:
            result = m1obs.review_m1_book(payload, now=NOW)
        mocked.assert_not_called()
        self.assertEqual(result["status"], "NOT_EVALUATED")
        self.assertIn("UNSUPPORTED_LEGACY_PRECISION", result["reason_codes"])

    def test_boundary_zero_and_one_prices_do_not_call_legacy(self):
        payload = _payload(
            yes_bids=[["1", "3"]],
            no_bids=[["0", "5"]],
        )
        with patch.object(m1obs, "detect_binary_arb") as mocked:
            result = m1obs.review_m1_book(payload, now=NOW)
        mocked.assert_not_called()
        self.assertEqual(result["status"], "NOT_EVALUATED")
        self.assertIn("BOUNDARY_PRICE", result["reason_codes"])
        self.assertFalse(result["legacy_detector_called"])


class InvariantMockTests(unittest.TestCase):
    def test_positive_mocked_legacy_result_forces_block(self):
        with patch.object(m1obs, "detect_binary_arb", return_value=object()) as mocked:
            result = m1obs.review_m1_book(_payload(), now=NOW)
        mocked.assert_called_once()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["INVARIANT_REQUIRES_REVIEW"])
        self.assertTrue(result["legacy_detector_called"])
        _assert_flags_are_safe(self, result)


class SafetyFlagInvariantTests(unittest.TestCase):
    def test_flags_are_false_across_all_statuses(self):
        scenarios = [
            _payload(),
            _payload(yes_bids=[]),
            _payload(price_unit="EUR"),
            _payload(yes_bids=[["0.60", "4"]], no_bids=[["0.55", "4"]]),
            _payload(yes_bids=[["0.4567", "3"]], no_bids=[["0.10", "2"]]),
        ]
        for payload in scenarios:
            result = m1obs.review_m1_book(payload, now=NOW)
            _assert_flags_are_safe(self, result)


class NoExecutorImportTests(unittest.TestCase):
    def test_module_does_not_import_engine_executor_settings_or_client(self):
        source = Path(m1obs.__file__).read_text()
        forbidden = ("engine", "executor", "settings", "client", "websocket")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                lowered = stripped.lower()
                for token in forbidden:
                    self.assertNotIn(token, lowered, msg=f"forbidden import: {stripped!r}")


if __name__ == "__main__":
    unittest.main()
