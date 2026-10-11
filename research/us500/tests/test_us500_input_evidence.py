"""Regresiones de identidad y costes; todas las entradas son inventadas."""

import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_us500_session import observed_shape_fixture

from example_session import session
from us500_engine import DataError, body_digest, simulate_round_trip
from us500_runner import build_plan, replay
from us500_session import build_session


def metadata(data):
    for frame in data["frames"]:
        observation = frame["snapshot"]["observations"]["market"]
        observation["body"]["market"].update(
            market_id="fixture",
            market_version=1,
            exchange_index=1,
            asset_class="Indices",
        )
        observation["canonical_body_sha256"] = body_digest(observation["body"])
    return data


def changed(frame, **updates):
    observation = frame["snapshot"]["observations"]["market"]
    observation["body"]["market"].update(updates)
    observation["canonical_body_sha256"] = body_digest(observation["body"])


class InputEvidenceTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_contract_change_before_first_fill_blocks(self):
        for field, value in (
            ("market_id", "different"),
            ("market_version", 2),
            ("exchange_index", 2),
        ):
            data = metadata(session())
            changed(data["frames"][2], **{field: value})
            events, report = build_plan(data)
            with self.subTest(field=field):
                self.assertFalse(any(e["type"] == "fill" for e in events))
                self.assertEqual(
                    report["frames"][-1]["reason"], "INSTRUMENT_CHANGED_DURING_SESSION"
                )
                self.assertFalse(
                    report["economic_path_complete_under_explicit_assumptions"]
                )

    def test_contract_change_retains_existing_exposure_and_restart(self):
        data = metadata(session())
        changed(data["frames"][3], market_version=2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "isolated.sqlite"
            first = replay(data, path)
            self.assertEqual(first, replay(data, path))
        self.assertEqual(first["residual_position_quantity"], "2")
        self.assertTrue(first["final_state"]["paused"])
        self.assertEqual(first["input_provenance"]["processed_frames"], 4)
        self.assertEqual(first["input_provenance"]["requested_frames"], 10)

    def test_missing_metadata_allowed_only_for_legacy_synthetic(self):
        _, old = build_plan(session())
        self.assertEqual(old["final_state"]["cash_usd"], "98.6643")
        for observed, partial in ((True, False), (False, True)):
            data = session()
            if observed:
                for frame in data["frames"]:
                    frame["snapshot"]["synthetic"] = False
            if partial:
                changed(data["frames"][0], market_id="partial")
            events, report = build_plan(data)
            self.assertEqual(report["frames"][0]["status"], "DATA_BLOCKED")
            self.assertFalse(any(e["type"] == "fill" for e in events))

    def test_invalid_identifiers_and_asset_class_block(self):
        for key, value in (
            ("market_id", "../wrong"),
            ("market_version", True),
            ("market_version", 0),
            ("exchange_index", -1),
            ("exchange_index", True),
            ("asset_class", "Crypto"),
        ):
            data = metadata(session())
            changed(data["frames"][0], **{key: value})
            events, report = build_plan(data)
            with self.subTest(key=key, value=value):
                self.assertEqual(report["frames"][0]["status"], "DATA_BLOCKED")
                self.assertFalse(any(e["type"] == "fill" for e in events))

    def test_round_trip_cannot_bypass_contract_identity(self):
        data = metadata(session())
        changed(data["frames"][1], market_id="other")
        with self.assertRaisesRegex(DataError, "INSTRUMENT_CHANGED"):
            simulate_round_trip(
                data["frames"][0]["snapshot"],
                data["frames"][1]["snapshot"],
                direction="long",
                quantity="1",
                fee_bps_per_leg="12",
                fee_quantum_usd="0.0001",
                slippage_bps_per_leg="1",
                funding_cashflow_usd="0",
            )

    def test_cost_contradictions_reject_before_creating_database(self):
        for field, value in (
            ("funding_cashflow_usd", "100"),
            ("infrastructure_cost_usd", "0"),
        ):
            data = build_session(**observed_shape_fixture())
            data["frames"][3][field] = value
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "never.sqlite"
                with self.assertRaisesRegex(
                    DataError, "INTERVAL_COST_EVIDENCE_MISMATCH"
                ):
                    replay(data, path)
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_cost_contradiction_cannot_modify_prior_database(self):
        valid = build_session(**observed_shape_fixture())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "existing.sqlite"
            replay(valid, path)
            before = path.read_bytes()
            changed_data = copy.deepcopy(valid)
            changed_data["frames"][3]["funding_cashflow_usd"] = "100"
            with self.assertRaisesRegex(DataError, "INTERVAL_COST_EVIDENCE_MISMATCH"):
                replay(changed_data, path)
            self.assertEqual(path.read_bytes(), before)

    def test_cost_time_note_shape_and_missing_evidence_rejected(self):
        for field, value in (
            ("from_ms", 1791744840000),
            ("at_ms", 1791744840000),
            ("provenance_note", ""),
            ("unknown", "x"),
        ):
            data = build_session(**observed_shape_fixture())
            data["frames"][3]["snapshot"]["session_interval_cost_evidence"][field] = (
                value
            )
            with self.subTest(field=field), self.assertRaises(DataError):
                build_plan(data)
        data = build_session(**observed_shape_fixture())
        del data["frames"][0]["snapshot"]["session_interval_cost_evidence"]
        with self.assertRaisesRegex(DataError, "INTERVAL_COST_EVIDENCE_REQUIRED"):
            build_plan(data)

    def test_standalone_cost_evidence_also_cannot_contradict_frame(self):
        data = build_session(**observed_shape_fixture())
        del data["assembly"]
        data["frames"][3]["funding_cashflow_usd"] = "100"
        with self.assertRaisesRegex(DataError, "INTERVAL_COST_EVIDENCE_MISMATCH"):
            build_plan(data)

    def test_assembly_hash_and_contents_cannot_contradict_captures(self):
        for rehash in (False, True):
            data = build_session(**observed_shape_fixture())
            data["assembly"]["instrument_metadata"]["market_version"] = 2
            if rehash:
                data["assembly"]["instrument_metadata_sha256"] = body_digest(
                    data["assembly"]["instrument_metadata"]
                )
            with self.subTest(rehash=rehash), self.assertRaises(DataError):
                build_plan(data)

    def test_provenance_never_certifies_synthetic_or_observed_claims(self):
        for data in (session(), build_session(**observed_shape_fixture())):
            _, report = build_plan(data)
            provenance = report["input_provenance"]
            self.assertEqual(provenance["requested_frames"], len(data["frames"]))
            self.assertEqual(
                provenance["synthetic_frames_declared"]
                + provenance["observed_frames_declared"],
                len(data["frames"]),
            )
            self.assertFalse(provenance["source_authenticity_verified"])
            self.assertFalse(provenance["complete_market_session_verified"])
            self.assertFalse(provenance["independence_verified"])
            self.assertEqual(report["independent_settled_samples"], 0)
            self.assertFalse(report["strategy_validated"])
            self.assertLess(Decimal(report["final_state"]["net_pnl_usd"]), 0)

    def test_malformed_assembly_metadata_is_sanitized(self):
        for malformed in (None, [], "wrong", {}):
            data = build_session(**observed_shape_fixture())
            data["assembly"]["instrument_metadata"] = malformed
            data["assembly"]["instrument_metadata_sha256"] = body_digest(malformed)
            with self.subTest(metadata=malformed), self.assertRaises(DataError):
                build_plan(data)
        data = build_session(**observed_shape_fixture())
        del data["assembly"]["instrument_metadata"]
        data["assembly"]["instrument_metadata_sha256"] = body_digest(None)
        with self.assertRaisesRegex(DataError, "INVALID_SESSION_ASSEMBLY"):
            build_plan(data)


if __name__ == "__main__":
    unittest.main()
