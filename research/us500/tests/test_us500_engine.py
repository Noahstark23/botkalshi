"""Offline integrity and explicit scenario tests; all market fixtures are synthetic."""

import copy
import hashlib
import json
import tempfile
import unittest
import urllib.error
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

import us500_engine as engine

NOW = 1791744840000


def sample(*, at=NOW, environment="public", bids=None, asks=None):
    """A synthetic, complete, contemporaneous capture with distinct venue identities."""
    host, ticker = engine.HOSTS[environment]
    market = {
        "ticker": ticker,
        "status": "active",
        "schedule": {"is_open": True},
        "tick_size": "0.0001",
        "contract_size": "0.001000",
        "underlying_multiplier": "1.000000",
        "fractional_trading_enabled": False,
        "reference_price": {"price": "13.5000", "ts_ms": at - 500},
    }
    observations = {}
    for name, body, end, suffix in (
        ("market", {"market": market}, at - 10, ""),
        (
            "orderbook",
            {
                "orderbook": {
                    "bids": bids if bids is not None else [["13.4000", "20.00"]],
                    "asks": asks if asks is not None else [["13.6000", "20.00"]],
                }
            },
            at,
            "/orderbook?depth=20",
        ),
    ):
        raw = json.dumps(body).encode()
        observations[name] = {
            "url": f"{host}/trade-api/v2/margin/markets/{ticker}{suffix}",
            "http_status": 200,
            "requested_at_ms": at - 20,
            "received_at_ms": end,
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "body": body,
        }
    return {
        "schema": engine.SCHEMA,
        "environment": environment,
        "ticker": ticker,
        "synthetic": True,
        "observations": observations,
        "atomic_snapshot": False,
        "authenticated": False,
    }


def market(snapshot):
    return snapshot["observations"]["market"]["body"]["market"]


def book(snapshot):
    return snapshot["observations"]["orderbook"]["body"]["orderbook"]


def scenario(entry, exit_snapshot, **overrides):
    assumptions = {
        "direction": "long",
        "quantity": "1",
        "fee_bps_per_leg": "0",
        "fee_quantum_usd": "0.01",
        "slippage_bps_per_leg": "0",
        "funding_cashflow_usd": "0",
    }
    assumptions.update(overrides)
    return engine.simulate_round_trip(entry, exit_snapshot, **assumptions)


class DecimalAndJsonTests(unittest.TestCase):
    def test_decimal_text_preserves_precision(self):
        self.assertEqual(
            engine.number("999999999999999999.123456789012"),
            Decimal("999999999999999999.123456789012"),
        )

    def test_money_rejects_nonfinite_floats_booleans_and_ambiguous_notation(self):
        for value in (
            "NaN",
            "Infinity",
            "-Infinity",
            "1e2",
            " 1",
            "01",
            ".1",
            0.1,
            1,
            True,
            None,
        ):
            with self.subTest(value=repr(value)), self.assertRaises(engine.DataError):
                engine.number(value)

    def test_json_rejects_nonfinite_numbers_at_any_depth(self):
        for raw in (b'{"a":NaN}', b'{"a":[Infinity]}', b'{"a":{"b":-Infinity}}'):
            with self.subTest(raw=raw), self.assertRaises(engine.DataError):
                engine.decode(raw)

    def test_duplicate_json_key_is_rejected_including_nested_market(self):
        for raw in (
            b'{"ticker":"A","ticker":"B"}',
            b'{"market":{"price":"1","price":"2"}}',
        ):
            with self.subTest(raw=raw), self.assertRaises(engine.DataError):
                engine.decode(raw)

    def test_json_numeric_decimal_cannot_sneak_into_money_fields(self):
        data = sample()
        market(data)["tick_size"] = 0.0001
        decoded = engine.decode(json.dumps(data).encode())
        self.assertIsInstance(market(decoded)["tick_size"], Decimal)
        with self.assertRaises(engine.DataError):
            engine.normalize(decoded, at_ms=NOW)

    def test_json_requires_bounded_object(self):
        for raw in (
            b"[]",
            b"null",
            b'"object"',
            b"\xff",
            b"{" + b" " * engine.MAX_BYTES,
        ):
            with self.subTest(length=len(raw)), self.assertRaises(engine.DataError):
                engine.decode(raw)


class SnapshotTests(unittest.TestCase):
    def test_body_change_after_digest_is_rejected(self):
        for resource in ("market", "orderbook"):
            data = sample()
            for observation in data["observations"].values():
                observation["canonical_body_sha256"] = engine.body_digest(
                    observation["body"]
                )
            if resource == "market":
                market(data)["contract_size"] = "0.002000"
            else:
                book(data)["asks"][0][1] = "19.00"
            with (
                self.subTest(resource=resource),
                self.assertRaisesRegex(engine.DataError, "BODY_DIGEST_MISMATCH"),
            ):
                engine.normalize(data, at_ms=NOW)

    def test_complete_valid_digests_verify_payload_integrity_but_not_origin(self):
        data = sample()
        for observation in data["observations"].values():
            observation["canonical_body_sha256"] = engine.body_digest(
                observation["body"]
            )
        normalized = engine.normalize(data, at_ms=NOW)
        self.assertTrue(normalized["payload_integrity_checked"])
        self.assertFalse(normalized["source_authenticity_verified"])

    def test_legacy_and_partially_hashed_captures_do_not_claim_integrity(self):
        legacy = sample()
        partial = sample()
        observation = partial["observations"]["market"]
        observation["canonical_body_sha256"] = engine.body_digest(observation["body"])
        for data in (legacy, partial):
            with self.subTest(partially_hashed=data is partial):
                normalized = engine.normalize(data, at_ms=NOW)
                self.assertFalse(normalized["payload_integrity_checked"])
                self.assertFalse(normalized["source_authenticity_verified"])

    def test_individual_list_or_null_observations_fail_without_unhandled_exception(
        self,
    ):
        for resource in ("market", "orderbook"):
            for invalid in ([], None):
                data = sample()
                data["observations"][resource] = invalid
                with self.subTest(resource=resource, invalid=invalid):
                    with self.assertRaises(engine.DataError):
                        engine.normalize(data, at_ms=NOW)
                    report = engine.assess(data, at_ms=NOW)
                    self.assertEqual(report["data_state"], "INCOMPLETE_OR_UNUSABLE")
                    self.assertNotIn("observation", report)

    def test_unsorted_levels_normalize_best_bid_and_ask_before_spread(self):
        data = sample(
            bids=[["13.1", "2"], ["13.4", "3"], ["13.2", "1"]],
            asks=[["13.9", "1"], ["13.6", "4"], ["13.7", "2"]],
        )
        normalized = engine.normalize(data, at_ms=NOW)
        self.assertEqual(
            [p for p, _ in normalized["bids"]],
            list(map(Decimal, ("13.4", "13.2", "13.1"))),
        )
        self.assertEqual(
            [p for p, _ in normalized["asks"]],
            list(map(Decimal, ("13.6", "13.7", "13.9"))),
        )
        self.assertEqual(normalized["mid_usd_per_api_unit"], Decimal("13.5"))
        self.assertEqual(normalized["basis_bps"], Decimal(0))

    def test_demo_has_different_ticker_and_source(self):
        normalized = engine.normalize(sample(environment="demo"), at_ms=NOW)
        self.assertEqual(normalized["ticker"], "KXUS500PERP1")
        self.assertEqual(normalized["environment"], "demo")
        wrong = sample(environment="demo")
        wrong["ticker"] = "KXUS500PERP"
        with self.assertRaises(engine.DataError):
            engine.normalize(wrong, at_ms=NOW)

    def test_market_body_cannot_claim_a_different_instrument(self):
        data = sample()
        market(data)["ticker"] = "KXBTCPERP"
        with self.assertRaises(engine.DataError):
            engine.normalize(data, at_ms=NOW)

    def test_active_but_schedule_closed_is_unusable(self):
        data = sample()
        market(data)["schedule"]["is_open"] = False
        report = engine.assess(data, at_ms=NOW)
        self.assertEqual(report["data_state"], "INCOMPLETE_OR_UNUSABLE")
        self.assertEqual(report["reason"], "MARKET_CLOSED")
        self.assertNotIn("observation", report)

    def test_closed_inactive_and_missing_schedule_fail(self):
        for status in ("closed", "inactive"):
            data = sample()
            market(data)["status"] = status
            with self.subTest(status=status), self.assertRaises(engine.DataError):
                engine.normalize(data, at_ms=NOW)
        data = sample()
        market(data)["schedule"] = None
        with self.assertRaises(engine.DataError):
            engine.normalize(data, at_ms=NOW)

    def test_stale_and_future_capture_cannot_be_treated_as_current(self):
        for at in (NOW + engine.MAX_AGE_MS + 1, NOW - 1):
            with self.subTest(at=at), self.assertRaises(engine.DataError):
                engine.normalize(sample(), at_ms=at)

    def test_slow_or_backwards_request_clock_is_rejected(self):
        for start in (NOW - engine.MAX_AGE_MS - 1, NOW + 1):
            data = sample()
            data["observations"]["orderbook"]["requested_at_ms"] = start
            with self.subTest(start=start), self.assertRaises(engine.DataError):
                engine.normalize(data, at_ms=NOW)

    def test_future_reference_is_rejected_and_stale_reference_does_not_create_basis(
        self,
    ):
        future = sample()
        market(future)["reference_price"]["ts_ms"] = NOW + 1
        with self.assertRaises(engine.DataError):
            engine.normalize(future, at_ms=NOW)
        stale = sample()
        market(stale)["reference_price"]["ts_ms"] = NOW - 60001
        normalized = engine.normalize(stale, at_ms=NOW)
        self.assertEqual(normalized["reference_state"], "STALE")
        self.assertIsNone(normalized["basis_bps"])

    def test_snapshot_source_must_match_environment_and_resource(self):
        for source in (
            "https://example.com/trade-api/v2/margin/markets/KXUS500PERP",
            "https://external-api.demo.kalshi.co/trade-api/v2/margin/markets/KXUS500PERP1",
            "https://external-api.kalshi.com/trade-api/v2/markets/KXUS500PERP",
        ):
            data = sample()
            data["observations"]["market"]["url"] = source
            with self.subTest(source=source), self.assertRaises(engine.DataError):
                engine.normalize(data, at_ms=NOW)

    def test_duplicate_offtick_fractional_zero_and_extreme_levels_are_rejected(self):
        invalid = (
            [["13.4", "1"], ["13.4000", "2"]],
            [["13.40001", "1"]],
            [["13.4", "0.5"]],
            [["13.4", "0"]],
            [["0", "1"]],
            [["922337203685477.5807", "1"]],
            [],
        )
        for levels in invalid:
            data = sample(asks=copy.deepcopy(levels))
            with self.subTest(levels=levels), self.assertRaises(engine.DataError):
                engine.normalize(data, at_ms=NOW)

    def test_locked_and_crossed_books_rejected(self):
        for ask in ("13.4", "13.3"):
            with self.subTest(ask=ask), self.assertRaises(engine.DataError):
                engine.normalize(sample(asks=[[ask, "3"]]), at_ms=NOW)

    def test_provenance_and_clock_do_not_accept_bool_or_seconds(self):
        for time in (True, NOW // 1000, float(NOW)):
            with self.subTest(time=time), self.assertRaises(engine.DataError):
                engine.normalize(sample(), at_ms=time)
        data = sample()
        data["synthetic"] = "true"
        with self.assertRaises(engine.DataError):
            engine.normalize(data, at_ms=NOW)


class ScenarioTests(unittest.TestCase):
    def test_long_walks_multiple_levels_charges_both_fees_and_paid_funding(self):
        entry = sample(bids=[["12.9", "20"]], asks=[["14", "2"], ["13", "1"]])
        exit_data = sample(
            at=NOW + 10000, bids=[["14.5", "2"], ["15", "2"]], asks=[["15.2", "20"]]
        )
        result = scenario(
            entry,
            exit_data,
            quantity="3",
            fee_bps_per_leg="12",
            funding_cashflow_usd="-0.03",
        )
        self.assertEqual(result["entry_notional_usd"], Decimal(41))
        self.assertEqual(result["exit_notional_usd"], Decimal("44.5"))
        self.assertEqual(result["gross_at_observed_book_usd"], Decimal("3.5"))
        self.assertEqual(result["fees_usd"], Decimal("0.11"))
        self.assertEqual(
            result["net_before_margin_and_infrastructure_usd"], Decimal("3.36")
        )

    def test_short_uses_entry_bids_exit_asks_and_received_funding(self):
        entry = sample(bids=[["13", "1"], ["14", "2"]], asks=[["14.2", "20"]])
        exit_data = sample(
            at=NOW + 10000, bids=[["11.9", "20"]], asks=[["12.5", "2"], ["12", "1"]]
        )
        result = scenario(
            entry,
            exit_data,
            direction="short",
            quantity="3",
            fee_bps_per_leg="12",
            funding_cashflow_usd="0.02",
        )
        self.assertEqual(result["entry_notional_usd"], Decimal(41))
        self.assertEqual(result["exit_notional_usd"], Decimal(37))
        self.assertEqual(result["fees_usd"], Decimal("0.10"))
        self.assertEqual(
            result["net_before_margin_and_infrastructure_usd"], Decimal("3.92")
        )

    def test_adverse_slippage_applies_both_legs_and_changes_fee_notional(self):
        for direction in ("long", "short"):
            if direction == "long":
                entry = sample(bids=[["99", "20"]], asks=[["100", "20"]])
                exit_data = sample(
                    at=NOW + 10000, bids=[["110", "20"]], asks=[["111", "20"]]
                )
            else:
                entry = sample(bids=[["110", "20"]], asks=[["111", "20"]])
                exit_data = sample(
                    at=NOW + 10000, bids=[["99", "20"]], asks=[["100", "20"]]
                )
            with self.subTest(direction=direction):
                result = scenario(
                    entry,
                    exit_data,
                    direction=direction,
                    quantity="2",
                    fee_bps_per_leg="100",
                    slippage_bps_per_leg="100",
                )
                self.assertEqual(result["gross_at_observed_book_usd"], Decimal(20))
                self.assertEqual(result["slippage_cost_usd"], Decimal("4.2"))
                self.assertEqual(result["fees_usd"], Decimal("4.20"))
                self.assertEqual(
                    result["net_before_margin_and_infrastructure_usd"], Decimal("11.60")
                )

    def test_fees_round_up_each_leg_instead_of_rounding_combined_fee(self):
        entry = sample(bids=[["0.9", "20"]], asks=[["1", "20"]])
        exit_data = sample(at=NOW + 10000, bids=[["1", "20"]], asks=[["1.1", "20"]])
        result = scenario(entry, exit_data, fee_bps_per_leg="1")
        self.assertEqual(result["fees_usd"], Decimal("0.02"))
        self.assertEqual(
            result["net_before_margin_and_infrastructure_usd"], Decimal("-0.02")
        )

    def test_contract_size_and_multiplier_are_not_applied_twice_to_api_unit_prices(
        self,
    ):
        for size, multiplier in (("0.001", "1"), ("0.01", "2")):
            entry = sample()
            exit_data = sample(
                at=NOW + 10000, bids=[["14", "20"]], asks=[["14.2", "20"]]
            )
            for data in (entry, exit_data):
                market(data)["contract_size"] = size
                market(data)["underlying_multiplier"] = multiplier
            with self.subTest(size=size, multiplier=multiplier):
                result = scenario(entry, exit_data, quantity="2")
                self.assertEqual(result["entry_notional_usd"], Decimal("27.2"))
                self.assertEqual(result["exit_notional_usd"], Decimal(28))
                self.assertEqual(
                    result["net_before_margin_and_infrastructure_usd"], Decimal("0.8")
                )

    def test_insufficient_depth_on_either_leg_never_assumes_full_fill(self):
        for direction, side, leg in (
            ("long", "asks", "entry"),
            ("long", "bids", "exit"),
            ("short", "bids", "entry"),
            ("short", "asks", "exit"),
        ):
            entry, exit_data = sample(), sample(at=NOW + 10000)
            data = entry if leg == "entry" else exit_data
            book(data)[side][0][1] = "1"
            with (
                self.subTest(direction=direction, leg=leg),
                self.assertRaisesRegex(engine.DataError, "INSUFFICIENT_VISIBLE_DEPTH"),
            ):
                scenario(entry, exit_data, quantity="2", direction=direction)

    def test_unchanged_market_loses_the_spread_in_either_direction(self):
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                result = scenario(
                    sample(), sample(at=NOW + 10000), direction=direction, quantity="2"
                )
                self.assertEqual(
                    result["net_before_margin_and_infrastructure_usd"], Decimal("-0.4")
                )

    def test_funding_is_required_instead_of_defaulting_to_zero(self):
        with self.assertRaises(TypeError):
            engine.simulate_round_trip(
                sample(),
                sample(at=NOW + 10000),
                direction="long",
                quantity="1",
                fee_bps_per_leg="0",
                fee_quantum_usd="0.01",
                slippage_bps_per_leg="0",
            )
        with self.assertRaises(engine.DataError):
            scenario(sample(), sample(at=NOW + 10000), funding_cashflow_usd=None)

    def test_fractional_quantity_requires_both_snapshots_to_allow_it(self):
        entry, exit_data = sample(), sample(at=NOW + 10000)
        with self.assertRaises(engine.DataError):
            scenario(entry, exit_data, quantity="0.5")
        for data in (entry, exit_data):
            market(data)["fractional_trading_enabled"] = True
        result = scenario(entry, exit_data, quantity="0.5")
        self.assertEqual(
            result["net_before_margin_and_infrastructure_usd"], Decimal("-0.1")
        )

    def test_mismatched_contract_or_provenance_rejects_comparison(self):
        for key, replacement in (
            ("contract_size", "0.01"),
            ("tick_size", "0.0002"),
            ("underlying_multiplier", "2"),
            ("fractional_trading_enabled", True),
        ):
            entry, exit_data = sample(), sample(at=NOW + 10000)
            market(exit_data)[key] = replacement
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    engine.DataError, "INCOMPATIBLE_SCENARIO_SNAPSHOTS"
                ),
            ):
                scenario(entry, exit_data)
        exit_data = sample(at=NOW + 10000)
        exit_data["synthetic"] = False
        with self.assertRaisesRegex(
            engine.DataError, "INCOMPATIBLE_SCENARIO_SNAPSHOTS"
        ):
            scenario(sample(), exit_data)
        with self.assertRaisesRegex(
            engine.DataError, "INCOMPATIBLE_SCENARIO_SNAPSHOTS"
        ):
            scenario(sample(), sample(at=NOW + 10000, environment="demo"))

    def test_reverse_or_equal_time_is_not_a_round_trip(self):
        for exit_at in (NOW, NOW - 10000):
            with self.subTest(exit_at=exit_at), self.assertRaises(engine.DataError):
                scenario(sample(), sample(at=exit_at))

    def test_invalid_costs_quantities_and_direction_rejected(self):
        for overrides in (
            {"quantity": "0"},
            {"quantity": "-1"},
            {"quantity": 1.0},
            {"fee_bps_per_leg": "-0.1"},
            {"fee_bps_per_leg": "10001"},
            {"slippage_bps_per_leg": "-1"},
            {"slippage_bps_per_leg": "10000"},
            {"fee_quantum_usd": "0"},
            {"direction": "buy"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(engine.DataError):
                scenario(sample(), sample(at=NOW + 10000), **overrides)

    def test_malformed_observations_fail_with_data_error(self):
        for invalid in ([], None, "invalid"):
            for leg in ("entry", "exit"):
                entry, exit_data = sample(), sample(at=NOW + 10000)
                data = entry if leg == "entry" else exit_data
                data["observations"] = invalid
                with (
                    self.subTest(invalid=invalid, leg=leg),
                    self.assertRaises(engine.DataError),
                ):
                    scenario(entry, exit_data)

    def test_extra_observation_cannot_influence_scenario_time(self):
        entry, exit_data = sample(), sample(at=NOW + 10000)
        expected = scenario(entry, exit_data)
        entry["observations"]["untrusted_extra"] = {"received_at_ms": 4102444800000}
        exit_data["observations"]["untrusted_extra"] = {"received_at_ms": True}
        self.assertEqual(scenario(entry, exit_data), expected)

    def test_large_decimal_scenario_conserves_every_cost_without_context_roundoff(self):
        quantity = "999999999999999999"
        entry = sample(bids=[["13.4", quantity]], asks=[["13.6789", quantity]])
        exit_data = sample(
            at=NOW + 10000, bids=[["14.5678", quantity]], asks=[["14.6", quantity]]
        )
        result = scenario(
            entry,
            exit_data,
            quantity=quantity,
            slippage_bps_per_leg="123.456789012345",
            fee_bps_per_leg="12.345678901234",
            fee_quantum_usd="0.000000000001",
            funding_cashflow_usd="-0.123456789012",
        )
        with localcontext() as context:
            context.prec = 80
            expected_slippage = (
                (Decimal("13.6789") + Decimal("14.5678"))
                * Decimal(quantity)
                * Decimal("123.456789012345")
                / Decimal(10000)
            )
            self.assertEqual(result["slippage_cost_usd"], expected_slippage)
            self.assertEqual(
                result["net_before_margin_and_infrastructure_usd"],
                result["gross_at_observed_book_usd"]
                - result["slippage_cost_usd"]
                - result["fees_usd"]
                + result["funding_cashflow_usd"],
            )


class FakeResponse:
    def __init__(self, raw=b"{}", status=200):
        self.raw = raw
        self.status = status
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        self.read_sizes.append(size)
        return self.raw[:size]


class EvidenceFileTests(unittest.TestCase):
    def test_new_evidence_roundtrips_without_binary_float_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "evidence.json"
            engine.save_new(
                destination, {"amount": Decimal("13.456789012345"), "synthetic": True}
            )
            self.assertEqual(
                json.loads(destination.read_text()),
                {"amount": "13.456789012345", "synthetic": True},
            )

    def test_existing_evidence_is_not_replaced_or_truncated(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "evidence.json"
            original = b'{"immutable":"original"}\n'
            destination.write_bytes(original)
            with self.assertRaises(FileExistsError):
                engine.save_new(destination, {"immutable": "changed"})
            self.assertEqual(destination.read_bytes(), original)

    def test_symlink_cannot_overwrite_existing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original.json"
            original.write_text("unchanged")
            destination = Path(directory) / "link.json"
            destination.symlink_to(original)
            with self.assertRaises(FileExistsError):
                engine.save_new(destination, {"tamper": True})
            self.assertTrue(destination.is_symlink())
            self.assertEqual(original.read_text(), "unchanged")

    def test_dangling_symlink_cannot_create_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            destination = Path(directory) / "link.json"
            destination.symlink_to(missing)
            with self.assertRaises(FileExistsError):
                engine.save_new(destination, {"tamper": True})
            self.assertFalse(missing.exists())
            self.assertTrue(destination.is_symlink())


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class PublicReaderTests(unittest.TestCase):
    def setUp(self):
        # Any accidental real networking fails even if a test forgets its fake.
        self.socket_guard = patch(
            "socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN_IN_TEST")
        )
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)

    def reader(self, responses, environment="public"):
        transport = FakeTransport(responses)
        with patch.object(
            engine.urllib.request, "build_opener", return_value=transport
        ):
            reader = engine.PublicReader(environment)
        return reader, transport

    def test_capture_uses_two_exact_anonymous_get_resources(self):
        for env, host, ticker in (
            ("public", "https://external-api.kalshi.com", "KXUS500PERP"),
            ("demo", "https://external-api.demo.kalshi.co", "KXUS500PERP1"),
        ):
            with self.subTest(env=env):
                reader, transport = self.reader(
                    [FakeResponse(), FakeResponse()], environment=env
                )
                captured = reader.capture()
                expected = [
                    f"{host}/trade-api/v2/margin/markets/{ticker}",
                    f"{host}/trade-api/v2/margin/markets/{ticker}/orderbook?depth=20",
                ]
                self.assertEqual([r.full_url for r, _ in transport.calls], expected)
                for request, timeout in transport.calls:
                    self.assertEqual(request.get_method(), "GET")
                    self.assertIsNone(request.data)
                    self.assertEqual(
                        dict(request.header_items()), {"Accept": "application/json"}
                    )
                    self.assertEqual(timeout, 5)
                self.assertFalse(captured["authenticated"])
                self.assertFalse(captured["synthetic"])
                self.assertFalse(captured["atomic_snapshot"])
                self.assertEqual(
                    captured["observations"]["market"]["raw_sha256"],
                    hashlib.sha256(b"{}").hexdigest(),
                )
                self.assertEqual(
                    captured["observations"]["market"]["canonical_body_sha256"],
                    hashlib.sha256(b"{}").hexdigest(),
                )

    def test_arbitrary_url_order_path_and_account_resources_never_reach_transport(self):
        reader, transport = self.reader([])
        for resource in (
            "orders",
            "balance",
            "../../portfolio/orders",
            "https://example.com",
            "market?secret=x",
            "orderbook/../orders",
        ):
            with self.subTest(resource=resource), self.assertRaises(engine.DataError):
                reader.get(resource)
        self.assertEqual(transport.calls, [])

    def test_third_get_is_blocked_even_for_same_public_resource(self):
        reader, transport = self.reader([FakeResponse(), FakeResponse()])
        reader.get("market")
        reader.get("market")
        with self.assertRaisesRegex(engine.DataError, "REQUEST_BUDGET_EXHAUSTED"):
            reader.get("orderbook")
        self.assertEqual(len(transport.calls), 2)

    def test_failed_network_attempts_consume_budget_and_are_not_retried(self):
        reader, transport = self.reader(
            [urllib.error.URLError("private diagnostic"), FakeResponse()]
        )
        with self.assertRaisesRegex(engine.DataError, "PUBLIC_DATA_UNAVAILABLE"):
            reader.get("market")
        self.assertEqual(len(transport.calls), 1)
        reader.get("orderbook")
        with self.assertRaisesRegex(engine.DataError, "REQUEST_BUDGET_EXHAUSTED"):
            reader.get("market")
        self.assertEqual(len(transport.calls), 2)

    def test_oversized_and_bad_json_responses_fail_after_bounded_read(self):
        for raw in (b" " * (engine.MAX_BYTES + 10), b'{"a":1,"a":2}', b'{"a":NaN}'):
            response = FakeResponse(raw)
            reader, transport = self.reader([response])
            with self.subTest(length=len(raw)), self.assertRaises(engine.DataError):
                reader.get("market")
            self.assertEqual(response.read_sizes, [engine.MAX_BYTES + 1])
            self.assertEqual(len(transport.calls), 1)

    def test_non_success_status_and_redirect_are_never_accepted_as_capture(self):
        for status in (204, 302, 404, 429, 500):
            reader, transport = self.reader([FakeResponse(status=status)])
            with self.subTest(status=status), self.assertRaises(engine.DataError):
                reader.get("market")
            self.assertEqual(len(transport.calls), 1)
        with self.assertRaisesRegex(engine.DataError, "REDIRECT_REJECTED"):
            engine._NoRedirect().redirect_request(
                None, None, 302, "Found", {}, "https://example.com"
            )

    def test_invalid_environment_never_creates_reader(self):
        for environment in ("production", "https://example.com", "../demo"):
            with (
                self.subTest(environment=environment),
                self.assertRaises(engine.DataError),
            ):
                engine.PublicReader(environment)


if __name__ == "__main__":
    unittest.main()
