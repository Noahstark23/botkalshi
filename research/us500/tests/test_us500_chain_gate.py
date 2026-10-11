"""Adversarial offline tests; all manifests/statuses are invented fixtures."""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import us500_chain_gate as chain_gate
from example_session import NOW
from us500_engine import DataError, body_digest
from us500_window_capture import MAX_STORAGE_BYTES, PLAN_SCHEMA

_real_fstat = os.fstat


def root_owned_fstat(descriptor):
    """Simulate immutable deployment ownership without changing filesystem owners."""
    info = _real_fstat(descriptor)
    return SimpleNamespace(st_uid=0, st_mode=info.st_mode, st_size=info.st_size)


def chain(minutes=(3, 3, 3), start=NOW):
    segments, at = [], start
    for i, count in enumerate(minutes, 1):
        name = f"part{i:02d}"
        end = at + count * 60_000
        segments.append(
            {
                "name": name,
                "state_directory": "session" if i == 1 else name,
                "plan": {
                    "schema": PLAN_SCHEMA,
                    "collection_id": "invented-" + name,
                    "environment": "public",
                    "ticker": "KXUS500PERP",
                    "start_ms": at,
                    "end_ms": end,
                    "deadline_ms": end,
                    "cadence_ms": 60_000,
                    "max_slots": count,
                    "max_gets": count * 2,
                    "max_storage_bytes": MAX_STORAGE_BYTES,
                },
            }
        )
        at = end
    return {
        "schema": chain_gate.CHAIN_SCHEMA,
        "chain_id": "invented-chain",
        "market_open_ms": start,
        "observed_close_ms": at - 60_000,
        "discovery_gets": 1,
        "max_gets": sum(minutes) * 2 + 1,
        "segments": segments,
    }


def status(plan, *, counts=None, state="COMPLETE", reason="ALL_SLOTS_FINISHED"):
    counts = counts or {"USABLE": plan["max_slots"]}
    reserved = 2 * sum(
        counts.get(key, 0)
        for key in ("USABLE", "INVALID", "CLOSED", "IN_PROGRESS", "INTERRUPTED_UNKNOWN")
    )
    return {
        "schema": "us500-public-window-status-v1",
        "collection_id": plan["collection_id"],
        "plan_sha256": body_digest(plan),
        "status": state,
        "reason": reason,
        "expected_slots": plan["max_slots"],
        "slot_counts": counts,
        "reserved_gets": reserved,
        "attempted_gets": reserved,
        "unused_reserved_gets": 0,
        "max_gets": plan["max_gets"],
        "instrument_metadata_sha256": "a" * 64,
        "full_planned_slot_coverage": counts.get("USABLE", 0) == plan["max_slots"],
        "retained_artifacts_integrity_checked": True,
        "slot_counts_meaning": "classification_when_saved",
        "continuous_market_history_verified": False,
        "source_authenticity_verified": False,
        "independence_verified": False,
        "strategy_validated": False,
        "execution_available": False,
        "actual_orders": 0,
        "status_written_at_ms": plan["end_ms"] - 60_000 + 5_000,
    }


class ChainGateTests(unittest.TestCase):
    def setUp(self):
        for target in (
            "socket.socket",
            "sqlite3.connect",
            "us500_window_capture.WindowCapture",
        ):
            guard = patch(target, side_effect=AssertionError("FORBIDDEN_SIDE_EFFECT"))
            guard.start()
            self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = chain()
        self.current = self.manifest["segments"][1]["plan"]

    def save_status(self, index, data=None):
        part = self.manifest["segments"][index]
        path = self.root / part["state_directory"] / "status.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(data or status(part["plan"])))
        return path

    def evaluate(self, *, target="part02", now=None, supplied=None):
        current = self.manifest["segments"][int(target[-2:]) - 1]["plan"]
        return chain_gate.gate(
            self.manifest,
            target,
            supplied or current,
            self.root,
            now_ms=current["start_ms"] + 5_000 if now is None else now,
        )

    def test_happy_path_is_read_only_and_never_claims_full_session(self):
        path = self.save_status(0)
        before = path.read_bytes(), path.stat().st_mtime_ns
        result = self.evaluate()
        self.assertTrue(result["allowed"])
        self.assertFalse(result["prior_coverage_incomplete"])
        self.assertEqual(result["prior_segments_checked"], ["part01"])
        for key in (
            "full_session_verified",
            "source_authenticity_verified",
            "independence_verified",
            "strategy_validated",
            "execution_available",
        ):
            self.assertFalse(result[key])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(list(self.root.rglob("*")), [path.parent, path])

    def test_actual_five_segment_declared_budget_and_dates(self):
        data = chain((1500, 1500, 1500, 1500, 1141), start=1791756000000)
        data["chain_id"] = "us500-session-20261011T2200Z-full"
        checked = chain_gate.validate_chain(data)
        self.assertEqual(checked["observed_close_ms"], 1792184400000)
        self.assertEqual(checked["segments"][-1]["plan"]["end_ms"], 1792184460000)
        self.assertEqual(checked["max_gets"], 14283)
        self.assertEqual(sum(x["plan"]["max_gets"] for x in checked["segments"]), 14282)

    def test_incomplete_permitted_terminals_keep_gap_label(self):
        prior = self.manifest["segments"][0]["plan"]
        for reason in ("ALL_SLOTS_FINISHED", "WINDOW_ENDED", "DEADLINE_REACHED"):
            self.save_status(
                0,
                status(
                    prior,
                    counts={"USABLE": 1, "INVALID": 1, "MISSED": 1},
                    state="INCOMPLETE",
                    reason=reason,
                ),
            )
            with self.subTest(reason=reason):
                result = self.evaluate()
                self.assertTrue(result["allowed"])
                self.assertTrue(result["prior_coverage_incomplete"])
                self.assertEqual(
                    result["prior_gap_counts"], {"INVALID": 1, "MISSED": 1}
                )

    def test_every_prior_segment_is_required_not_just_adjacent(self):
        self.save_status(1)
        result = self.evaluate(target="part03")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["blocking_segment"], "part01")
        self.save_status(0)
        self.assertTrue(self.evaluate(target="part03")["allowed"])

    def test_known_instrument_hash_conflict_between_previous_segments_blocks(self):
        self.save_status(0)
        data = status(self.manifest["segments"][1]["plan"])
        data["instrument_metadata_sha256"] = "b" * 64
        self.save_status(1, data)
        result = self.evaluate(target="part03")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "PREVIOUS_INSTRUMENT_IDENTITY_CHANGED")

    def test_missing_instrument_for_all_missed_segment_does_not_invent_conflict(self):
        prior = self.manifest["segments"][0]["plan"]
        data = status(
            prior, counts={"MISSED": 3}, state="INCOMPLETE", reason="WINDOW_ENDED"
        )
        data["instrument_metadata_sha256"] = None
        self.save_status(0, data)
        self.save_status(1)
        result = self.evaluate(target="part03")
        self.assertTrue(result["allowed"])
        self.assertTrue(result["prior_coverage_incomplete"])
        self.assertEqual(result["prior_gap_counts"], {"MISSED": 3})
        self.assertFalse(result["full_session_verified"])

    def test_complete_slot_evidence_with_incomplete_finalization_remains_audited(self):
        # A crash can occur after the final slot commit but before the final
        # COMPLETE update; the end tick then consolidates WINDOW_ENDED.
        data = status(
            self.manifest["segments"][0]["plan"],
            state="INCOMPLETE",
            reason="WINDOW_ENDED",
        )
        self.save_status(0, data)
        result = self.evaluate()
        self.assertTrue(result["allowed"])
        self.assertEqual(result["prior_terminal_states"], {"part01": "INCOMPLETE"})
        self.assertFalse(result["full_session_verified"])

    def test_closed_or_unknown_or_waiting_never_allows_continuation(self):
        prior = self.manifest["segments"][0]["plan"]
        for kind in chain_gate.FORBIDDEN_SLOT_STATES:
            self.save_status(
                0, status(prior, counts={"USABLE": 2, kind: 1}, state="INCOMPLETE")
            )
            with self.subTest(kind=kind):
                result = self.evaluate()
                self.assertFalse(result["allowed"])
                self.assertEqual(result["reason"], "PREVIOUS_UNRESOLVED_OR_CLOSED_SLOT")

    def test_active_and_fault_terminals_never_allow(self):
        prior = self.manifest["segments"][0]["plan"]
        for state, reason in (
            ("ACTIVE", "ALL_SLOTS_FINISHED"),
            ("INCOMPLETE", "MARKET_CLOSED"),
            ("INCOMPLETE", "CLOCK_REGRESSION"),
            ("INCOMPLETE", "STORAGE_LIMIT"),
            ("INCOMPLETE", "LOCAL_STORAGE_ERROR"),
            ("INCOMPLETE", "EVIDENCE_INTEGRITY_FAILURE"),
        ):
            self.save_status(0, status(prior, state=state, reason=reason))
            with self.subTest(state=state, reason=reason):
                self.assertFalse(self.evaluate()["allowed"])

    def test_integrity_is_required_even_with_complete_label(self):
        data = status(self.manifest["segments"][0]["plan"])
        data["retained_artifacts_integrity_checked"] = False
        self.save_status(0, data)
        result = self.evaluate()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "PREVIOUS_EVIDENCE_INTEGRITY_UNCONFIRMED")

    def test_time_bounds_wait_and_one_final_local_minute(self):
        with patch(
            "us500_chain_gate._read_previous",
            side_effect=AssertionError("NO_READ_EXPECTED"),
        ):
            self.assertEqual(
                self.evaluate(now=self.current["start_ms"] - 1)["reason"],
                "BEFORE_SEGMENT_START",
            )
            self.assertEqual(
                self.evaluate(now=self.current["end_ms"] + 60_000)["reason"],
                "SEGMENT_WINDOW_EXPIRED",
            )
        self.save_status(0)
        for now in (
            self.current["start_ms"],
            self.current["end_ms"] - 1,
            self.current["end_ms"],
            self.current["end_ms"] + 59_999,
        ):
            with self.subTest(now=now):
                result = self.evaluate(now=now)
                self.assertTrue(result["allowed"])
                self.assertEqual(
                    result["local_finalization_only"], now >= self.current["end_ms"]
                )

    def test_original_segment_is_never_duplicated_by_chain_gate(self):
        result = self.evaluate(target="part01")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "FIRST_SEGMENT_SEPARATELY_MANAGED")

    def test_stale_and_future_status_are_blocked(self):
        prior = self.manifest["segments"][0]["plan"]
        now = self.current["start_ms"] + 5000
        for written in (prior["end_ms"] - 60_001, now + 5001):
            data = status(prior)
            data["status_written_at_ms"] = written
            self.save_status(0, data)
            self.assertFalse(self.evaluate(now=now)["allowed"])
        for written in (prior["end_ms"] - 60_000, now + 5000):
            data["status_written_at_ms"] = written
            self.save_status(0, data)
            self.assertTrue(self.evaluate(now=now)["allowed"])

    def test_external_plan_mismatch_even_when_both_are_valid(self):
        altered = copy.deepcopy(self.current)
        altered["collection_id"] = "another-collection"
        with self.assertRaisesRegex(DataError, "EXTERNAL_PLAN_MISMATCH"):
            self.evaluate(supplied=altered)

    def test_previous_identity_and_budget_mismatch_raise(self):
        prior = self.manifest["segments"][0]["plan"]
        changes = {
            "collection_id": "other",
            "plan_sha256": "0" * 64,
            "expected_slots": 5,
            "max_gets": 100,
            "actual_orders": True,
            "instrument_metadata_sha256": "unknown",
        }
        for field, value in changes.items():
            data = status(prior)
            data[field] = value
            self.save_status(0, data)
            with self.subTest(field=field), self.assertRaises(DataError):
                self.evaluate()

    def test_impossible_get_accounting_is_rejected(self):
        prior = self.manifest["segments"][0]["plan"]
        mutations = (
            {"attempted_gets": 0, "unused_reserved_gets": 6},
            {"reserved_gets": 4, "attempted_gets": 4},
            {"attempted_gets": 7},
            {"unused_reserved_gets": -1},
            {"reserved_gets": True},
        )
        for changes in mutations:
            data = status(prior)
            data.update(changes)
            self.save_status(0, data)
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(DataError, "INVALID_PREVIOUS_GET_ACCOUNTING"),
            ):
                self.evaluate()

    def test_malformed_counts_and_unsupported_claims_rejected(self):
        prior = self.manifest["segments"][0]["plan"]
        for counts in ({"USABLE": True}, {"USABLE": 4}, {"USABLE": -1}, {"BOGUS": 3}):
            data = status(prior)
            data["slot_counts"] = counts
            self.save_status(0, data)
            with self.assertRaises(DataError):
                self.evaluate()
        for field in chain_gate.FALSE_FLAGS:
            data = status(prior)
            data[field] = True
            self.save_status(0, data)
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(DataError, "UNSUPPORTED_PREVIOUS_STATUS_CLAIM"),
            ):
                self.evaluate()

    def test_contradictory_complete_label_is_rejected(self):
        prior = self.manifest["segments"][0]["plan"]
        self.save_status(0, status(prior, counts={"USABLE": 2, "MISSED": 1}))
        with self.assertRaisesRegex(DataError, "CONTRADICTORY_PREVIOUS_COVERAGE"):
            self.evaluate()

    def test_manifest_overlap_gap_duplicate_identity_and_caps(self):
        for change in (
            "overlap",
            "gap",
            "duplicate",
            "path",
            "order",
            "total",
            "discovery",
            "close",
        ):
            data = chain()
            if change in {"overlap", "gap"}:
                data["segments"][1]["plan"]["start_ms"] += (
                    -60_000 if change == "overlap" else 60_000
                )
            elif change == "duplicate":
                data["segments"][1]["plan"]["collection_id"] = data["segments"][0][
                    "plan"
                ]["collection_id"]
            elif change == "path":
                data["segments"][1]["state_directory"] = "../escape"
            elif change == "order":
                data["segments"][1]["name"] = "part03"
            elif change == "total":
                data["max_gets"] = 15001
            elif change == "discovery":
                data["discovery_gets"] = True
            else:
                data["observed_close_ms"] += 60_000
            with self.subTest(change=change), self.assertRaises(DataError):
                chain_gate.validate_chain(data)
        with self.assertRaises(DataError):
            chain_gate.validate_chain(chain((3,) * 9))
        oversized = chain((1500,) * 5)
        with self.assertRaises(DataError):
            chain_gate.validate_chain(oversized)

    def test_missing_state_is_wait_and_malformed_json_is_error(self):
        self.assertEqual(self.evaluate()["reason"], "PREVIOUS_STATUS_MISSING")
        path = self.save_status(0)
        path.write_text('{"schema":"one","schema":"two"}')
        with self.assertRaises(DataError):
            self.evaluate()
        path.write_bytes(b"x" * (chain_gate.MAX_PLAN_BYTES + 1))
        with self.assertRaisesRegex(DataError, "CHAIN_JSON_TOO_LARGE"):
            self.evaluate()

    def test_missing_digest_field_is_validation_error_even_with_zero_usable(self):
        data = status(
            self.manifest["segments"][0]["plan"],
            counts={"MISSED": 3},
            state="INCOMPLETE",
            reason="WINDOW_ENDED",
        )
        del data["instrument_metadata_sha256"]
        self.save_status(0, data)
        with self.assertRaisesRegex(DataError, "MISSING_PREVIOUS_INSTRUMENT_DIGEST"):
            self.evaluate()

    def test_file_and_directory_permissions_are_checked_without_chmod(self):
        path = self.save_status(0)
        path.chmod(0o666)
        with self.assertRaisesRegex(DataError, "CHAIN_JSON_UNSAFE_PERMISSIONS"):
            self.evaluate()
        self.assertEqual(path.stat().st_mode & 0o777, 0o666)
        path.chmod(0o600)
        path.parent.chmod(0o770)
        with self.assertRaisesRegex(DataError, "CHAIN_JSON_UNSAFE_PERMISSIONS"):
            self.evaluate()
        path.parent.chmod(0o700)
        self.assertTrue(self.evaluate()["allowed"])
        manifest_path = self.root / "manifest.json"
        manifest_path.write_text(json.dumps(self.manifest))
        manifest_path.chmod(0o644)
        with self.assertRaisesRegex(DataError, "CHAIN_JSON_UNSAFE_PERMISSIONS"):
            chain_gate.read_json(manifest_path, maximum=chain_gate.MAX_CHAIN_BYTES)
        manifest_path.chmod(0o444)
        with patch("us500_chain_gate.os.fstat", side_effect=root_owned_fstat):
            self.assertEqual(
                chain_gate.read_json(manifest_path, maximum=chain_gate.MAX_CHAIN_BYTES),
                self.manifest,
            )

    def test_immutable_requires_root_owner_even_when_service_owns_readonly_file(self):
        with patch("us500_chain_gate.os.geteuid", return_value=12345):
            service_owned = SimpleNamespace(st_uid=12345, st_mode=0o100444)
            with self.assertRaisesRegex(DataError, "CHAIN_JSON_UNSAFE_PERMISSIONS"):
                chain_gate._safe_permissions(service_owned, immutable=True)
            chain_gate._safe_permissions(service_owned, immutable=False)
            chain_gate._safe_permissions(
                SimpleNamespace(st_uid=0, st_mode=0o100444), immutable=True
            )

    def test_symlink_root_directory_file_and_fifo_refused(self):
        path = self.save_status(0)
        path.unlink()
        other = self.root / "other.json"
        other.write_text(json.dumps(status(self.manifest["segments"][0]["plan"])))
        path.symlink_to(other)
        with self.assertRaises(OSError):
            self.evaluate()
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(DataError, "CHAIN_JSON_REGULAR_FILE_REQUIRED"):
            self.evaluate()
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.evaluate()

    def test_cli_exit_zero_one_two_and_sanitizes_errors(self):
        manifest_path = self.root / "chain.json"
        plan_path = self.root / "plan.json"
        manifest_path.write_text(json.dumps(self.manifest))
        plan_path.write_text(json.dumps(self.current))
        manifest_path.chmod(0o444)
        plan_path.chmod(0o444)
        args = [
            "gate",
            "--chain",
            str(manifest_path),
            "--segment",
            "part02",
            "--plan",
            str(plan_path),
            "--state-root",
            str(self.root),
        ]
        with (
            patch("sys.argv", args),
            patch("us500_chain_gate.os.fstat", side_effect=root_owned_fstat),
            patch(
                "us500_chain_gate.time.time_ns",
                return_value=(self.current["start_ms"] + 5000) * 1_000_000,
            ),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(chain_gate.main(), 1)
            self.assertFalse(json.loads(out.getvalue())["allowed"])
            self.save_status(0)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(chain_gate.main(), 0)
            self.assertTrue(json.loads(out.getvalue())["allowed"])
            with patch(
                "us500_chain_gate.read_json",
                side_effect=OSError("SECRET_TOKEN https://private.example"),
            ):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(chain_gate.main(), 2)
                self.assertNotIn("SECRET_TOKEN", out.getvalue())
                self.assertNotIn("private.example", out.getvalue())


if __name__ == "__main__":
    unittest.main()
