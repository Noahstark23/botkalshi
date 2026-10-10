"""Unit tests for m5_shadow_fill_review.review_m5_fill. Pure stdlib, no network."""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import m5_shadow_fill_review as m5rev  # noqa: E402

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 22, 13, 30, 0, tzinfo=UTC)


def _fill(**overrides):
    base = {
        "id": 123,
        "side": "buy",
        "price_cents": 40,
        "count": 2,
        "fee_effective_cents": 4,
        "fee_model": "taker",
        "ticker": "KXTEST-26",
        "experiment_id": "exp-001",
        "metric_version": m5rev.METRIC_VERSION,
        "fee_source": "series",
        "fee_type": m5rev.FEE_TYPE,
        "fee_multiplier": 1,
    }
    base.update(overrides)
    return base


def _payload(**fill_overrides):
    return {
        "schema_version": m5rev.SCHEMA_VERSION_INPUT,
        "fill": _fill(**fill_overrides),
    }


def _assert_flags_are_safe(case, result):
    case.assertFalse(result["execution_authorized"])
    case.assertFalse(result["order_capability_present"])
    case.assertFalse(result["real_entry_eligible"])
    case.assertEqual(result["origin"], "M5")
    case.assertEqual(result["mode"], "SIMULATION_ONLY")
    case.assertEqual(result["capital_source"], "FICTIONAL_TEST_CAPITAL")
    case.assertEqual(result["evidence_state"], "DECLARED_FILL_NOT_LEDGER_VERIFIED")


class BuyFillTests(unittest.TestCase):
    def test_buy_40c_x2_with_verified_taker_fee_is_accepted_exact(self):
        result = m5rev.review_m5_fill(_payload(), now=NOW)
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["reason_codes"], [])
        # price_cents*count + fee = 40*2 + 4 = 84 cents.
        self.assertEqual(result["max_loss_usd"], "0.84")
        self.assertEqual(result["cycle_id"], "m5-shadow-fill-exp-001-123")
        self.assertEqual(result["idempotency_key"], "m5-shadow-fill-risk-exp-001-123")
        evidence = result["evidence"]
        self.assertEqual(evidence["exactness"], "EXACT_CONDITIONAL_ON_VALIDATED_DECLARED_EVIDENCE")
        self.assertEqual(evidence["fee_formula_provenance"]["recomputed_fee_cents"], 4)
        self.assertEqual(evidence["fee_formula_provenance"]["base_denominator"], 10_000)
        self.assertEqual(evidence["fee_multiplier"], "1")
        self.assertEqual(evidence["experiment_id"], "exp-001")
        self.assertEqual(evidence["metric_version"], m5rev.METRIC_VERSION)
        self.assertEqual(evidence["fee_type"], m5rev.FEE_TYPE)
        self.assertEqual(evidence["fee_source"], "series")
        self.assertEqual(evidence["ticker"], "KXTEST-26")
        _assert_flags_are_safe(self, result)


class SellFillTests(unittest.TestCase):
    def test_sell_40c_x2_with_verified_taker_fee_is_accepted_exact(self):
        result = m5rev.review_m5_fill(_payload(side="sell"), now=NOW)
        self.assertEqual(result["status"], "ACCEPTED")
        # (100-40)*count + fee = 60*2 + 4 = 124 cents.
        self.assertEqual(result["max_loss_usd"], "1.24")
        self.assertEqual(result["evidence"]["fee_formula_provenance"]["recomputed_fee_cents"], 4)
        _assert_flags_are_safe(self, result)


class MakerTakerMultiplierTests(unittest.TestCase):
    def test_maker_multiplier_one(self):
        result = m5rev.review_m5_fill(
            _payload(fee_model="maker", fee_effective_cents=1, fee_multiplier=1), now=NOW
        )
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["evidence"]["fee_formula_provenance"]["recomputed_fee_cents"], 1)
        self.assertEqual(result["evidence"]["fee_formula_provenance"]["base_denominator"], 40_000)

    def test_maker_multiplier_half(self):
        result = m5rev.review_m5_fill(
            _payload(fee_model="maker", fee_effective_cents=1, fee_multiplier=0.5), now=NOW
        )
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["evidence"]["fee_formula_provenance"]["recomputed_fee_cents"], 1)
        self.assertEqual(result["evidence"]["fee_multiplier"], "0.5")

    def test_taker_multiplier_half(self):
        result = m5rev.review_m5_fill(
            _payload(fee_model="taker", fee_effective_cents=2, fee_multiplier=0.5), now=NOW
        )
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["evidence"]["fee_formula_provenance"]["recomputed_fee_cents"], 2)

    def test_taker_multiplier_one_is_default_shape(self):
        result = m5rev.review_m5_fill(_payload(fee_model="taker", fee_multiplier=1), now=NOW)
        self.assertEqual(result["status"], "ACCEPTED")


class FeeMismatchTests(unittest.TestCase):
    def test_fee_mismatch_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_effective_cents=5), now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["FEE_EFFECTIVE_CENTS_INVALID"])
        self.assertIsNone(result["evidence"])
        self.assertIsNone(result["max_loss_usd"])

    def test_fee_zero_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_effective_cents=0), now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["FEE_EFFECTIVE_CENTS_INVALID"])

    def test_fee_missing_blocks(self):
        fill = _fill()
        del fill["fee_effective_cents"]
        result = m5rev.review_m5_fill({"schema_version": m5rev.SCHEMA_VERSION_INPUT, "fill": fill}, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("MISSING_FILL_FIELDS", result["reason_codes"])

    def test_fee_float_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_effective_cents=4.0), now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_FEE_EFFECTIVE_CENTS", result["reason_codes"])

    def test_fee_bool_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_effective_cents=True), now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_FEE_EFFECTIVE_CENTS", result["reason_codes"])


REQUIRED_FILL_FIELDS = (
    "id",
    "side",
    "price_cents",
    "count",
    "fee_effective_cents",
    "fee_model",
    "ticker",
    "experiment_id",
    "metric_version",
    "fee_source",
    "fee_type",
    "fee_multiplier",
)


class MissingRequiredFieldTests(unittest.TestCase):
    def test_each_required_field_missing_blocks(self):
        for field in REQUIRED_FILL_FIELDS:
            fill = _fill()
            del fill[field]
            payload = {"schema_version": m5rev.SCHEMA_VERSION_INPUT, "fill": fill}
            result = m5rev.review_m5_fill(payload, now=NOW)
            self.assertEqual(result["status"], "BLOCKED", msg=f"field={field}")
            self.assertIn("MISSING_FILL_FIELDS", result["reason_codes"], msg=f"field={field}")


class ExtraFieldTests(unittest.TestCase):
    def test_extra_field_blocks(self):
        payload = _payload(unexpected_field="value")
        result = m5rev.review_m5_fill(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("UNEXPECTED_FILL_FIELDS", result["reason_codes"])

    def test_extra_root_field_blocks(self):
        payload = _payload()
        payload["unexpected_root"] = "value"
        result = m5rev.review_m5_fill(payload, now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("UNEXPECTED_INPUT_FIELDS", result["reason_codes"])


class FloatAndBoolFieldTests(unittest.TestCase):
    def test_float_price_cents_blocks(self):
        result = m5rev.review_m5_fill(_payload(price_cents=40.0), now=NOW)
        self.assertIn("INVALID_PRICE_CENTS", result["reason_codes"])

    def test_float_count_blocks(self):
        result = m5rev.review_m5_fill(_payload(count=2.0), now=NOW)
        self.assertIn("INVALID_COUNT", result["reason_codes"])

    def test_bool_id_blocks(self):
        result = m5rev.review_m5_fill(_payload(id=True), now=NOW)
        self.assertIn("INVALID_FILL_ID", result["reason_codes"])

    def test_bool_price_cents_blocks(self):
        result = m5rev.review_m5_fill(_payload(price_cents=True), now=NOW)
        self.assertIn("INVALID_PRICE_CENTS", result["reason_codes"])

    def test_bool_count_blocks(self):
        result = m5rev.review_m5_fill(_payload(count=True), now=NOW)
        self.assertIn("INVALID_COUNT", result["reason_codes"])


class FeeMultiplierTests(unittest.TestCase):
    def test_negative_multiplier_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_multiplier=-1), now=NOW)
        self.assertIn("INVALID_FEE_MULTIPLIER", result["reason_codes"])

    def test_zero_multiplier_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_multiplier=0), now=NOW)
        self.assertIn("INVALID_FEE_MULTIPLIER", result["reason_codes"])

    def test_too_large_multiplier_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_multiplier=1_000_001), now=NOW)
        self.assertIn("INVALID_FEE_MULTIPLIER", result["reason_codes"])

    def test_non_numeric_string_multiplier_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_multiplier="abc"), now=NOW)
        self.assertIn("INVALID_FEE_MULTIPLIER", result["reason_codes"])

    def test_nan_multiplier_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_multiplier=float("nan")), now=NOW)
        self.assertIn("INVALID_FEE_MULTIPLIER", result["reason_codes"])

    def test_inf_multiplier_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_multiplier=float("inf")), now=NOW)
        self.assertIn("INVALID_FEE_MULTIPLIER", result["reason_codes"])

    def test_bool_multiplier_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_multiplier=True), now=NOW)
        self.assertIn("INVALID_FEE_MULTIPLIER", result["reason_codes"])

    def test_overlong_multiplier_string_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_multiplier="1" * 100), now=NOW)
        self.assertIn("INVALID_FEE_MULTIPLIER", result["reason_codes"])


class SchemaFieldValueTests(unittest.TestCase):
    def test_wrong_metric_version_blocks(self):
        result = m5rev.review_m5_fill(_payload(metric_version="f1-v1"), now=NOW)
        self.assertIn("INVALID_METRIC_VERSION", result["reason_codes"])

    def test_wrong_fee_type_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_type="linear"), now=NOW)
        self.assertIn("INVALID_FEE_TYPE", result["reason_codes"])

    def test_wrong_fee_source_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_source="guess"), now=NOW)
        self.assertIn("INVALID_FEE_SOURCE", result["reason_codes"])

    def test_event_override_fee_source_is_valid(self):
        result = m5rev.review_m5_fill(_payload(fee_source="event_override"), now=NOW)
        self.assertEqual(result["status"], "ACCEPTED")

    def test_invalid_ticker_blocks(self):
        result = m5rev.review_m5_fill(_payload(ticker="bad ticker!"), now=NOW)
        self.assertIn("INVALID_TICKER", result["reason_codes"])

    def test_invalid_experiment_id_blocks(self):
        result = m5rev.review_m5_fill(_payload(experiment_id=""), now=NOW)
        self.assertIn("INVALID_EXPERIMENT_ID", result["reason_codes"])

    def test_invalid_side_blocks(self):
        result = m5rev.review_m5_fill(_payload(side="hold"), now=NOW)
        self.assertIn("INVALID_SIDE", result["reason_codes"])

    def test_invalid_fee_model_blocks(self):
        result = m5rev.review_m5_fill(_payload(fee_model="market"), now=NOW)
        self.assertIn("INVALID_FEE_MODEL", result["reason_codes"])


class InputShapeTests(unittest.TestCase):
    def test_non_dict_input_blocks(self):
        result = m5rev.review_m5_fill(["not", "a", "dict"], now=NOW)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["INVALID_INPUT_SHAPE"])

    def test_wrong_schema_version_blocks(self):
        payload = _payload()
        payload["schema_version"] = "wrong-version"
        result = m5rev.review_m5_fill(payload, now=NOW)
        self.assertIn("INVALID_SCHEMA_VERSION", result["reason_codes"])

    def test_fill_not_a_dict_blocks(self):
        result = m5rev.review_m5_fill(
            {"schema_version": m5rev.SCHEMA_VERSION_INPUT, "fill": "not-a-dict"}, now=NOW
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("INVALID_FILL_SHAPE", result["reason_codes"])


class AuthorityFlagsTests(unittest.TestCase):
    def test_flags_false_on_accepted(self):
        _assert_flags_are_safe(self, m5rev.review_m5_fill(_payload(), now=NOW))

    def test_flags_false_on_blocked(self):
        _assert_flags_are_safe(self, m5rev.review_m5_fill(_payload(fee_effective_cents=0), now=NOW))


class ReplayAndConflictTests(unittest.TestCase):
    def test_identical_replay_is_deterministic(self):
        first = m5rev.review_m5_fill(_payload(), now=NOW)
        second = m5rev.review_m5_fill(_payload(), now=LATER)
        self.assertEqual(first["cycle_id"], second["cycle_id"])
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(first["max_loss_usd"], second["max_loss_usd"])

    def test_different_fill_id_yields_different_keys(self):
        first = m5rev.review_m5_fill(_payload(id=123), now=NOW)
        second = m5rev.review_m5_fill(_payload(id=456), now=NOW)
        self.assertNotEqual(first["cycle_id"], second["cycle_id"])
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])

    def test_different_experiment_id_yields_different_keys_for_same_fill_id(self):
        first = m5rev.review_m5_fill(_payload(experiment_id="exp-001"), now=NOW)
        second = m5rev.review_m5_fill(_payload(experiment_id="exp-002"), now=NOW)
        self.assertEqual(first["fill_id"], second["fill_id"])
        self.assertNotEqual(first["cycle_id"], second["cycle_id"])
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])

    def test_blocked_result_never_carries_a_collector_packet_id_alias(self):
        result = m5rev.review_m5_fill(_payload(fee_effective_cents=0), now=NOW)
        self.assertNotIn("packet_id", result)


class NoForbiddenImportTests(unittest.TestCase):
    def test_module_imports_are_restricted_to_stdlib_math_and_validation(self):
        tree = ast.parse((BASE / "m5_shadow_fill_review.py").read_text())
        allowed = {"__future__", "re", "datetime", "fractions", "typing"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertIn(node.module.split(".")[0], allowed)


if __name__ == "__main__":
    unittest.main()
