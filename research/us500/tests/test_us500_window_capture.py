"""Offline invented fixtures. These tests never collect market observations."""

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import us500_window_capture as window
from example_session import NOW, session
from us500_engine import DataError, body_digest


def plan(slots=3):
    return {
        "schema": window.PLAN_SCHEMA,
        "collection_id": "invented-test-window",
        "environment": "public",
        "ticker": "KXUS500PERP",
        "start_ms": NOW,
        "end_ms": NOW + slots * window.CADENCE_MS,
        "deadline_ms": NOW + slots * window.CADENCE_MS,
        "cadence_ms": 60_000,
        "max_slots": slots,
        "max_gets": 2 * slots,
        "max_storage_bytes": window.MAX_STORAGE_BYTES,
    }


class Feed:
    def __init__(self):
        self.now = NOW
        self.calls = []
        self.errors = {}
        self.closed = False
        self.malformed = False
        self.metadata_version = 1
        self.market_changes = {}
        self.before_get = None
        self.after_market = None

    def reader(self):
        return self

    def get(self, resource):
        if self.before_get:
            self.before_get(resource)
        self.calls.append(resource)
        if resource in self.errors:
            raise self.errors[resource]
        snapshot = session(environment="public")["frames"][0]["snapshot"]
        observation = copy.deepcopy(snapshot["observations"][resource])
        offset = 10 if resource == "market" else 30
        observation["requested_at_ms"] = self.now + offset
        observation["received_at_ms"] = self.now + offset + 10
        if resource == "market":
            market = observation["body"]["market"]
            market.update(
                market_id="fixture-market",
                market_version=self.metadata_version,
                exchange_index=1,
                asset_class="Indices",
            )
            market["reference_price"]["ts_ms"] = self.now
            market["liquidation_mark_price"]["ts_ms"] = self.now
            market["schedule"]["is_open"] = not self.closed
            market.update(self.market_changes)
        elif self.malformed:
            observation["body"]["orderbook"]["asks"] = []
        observation["canonical_body_sha256"] = body_digest(observation["body"])
        if resource == "market" and self.after_market:
            self.after_market()
        return observation


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.socket_guard = patch(
            "socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN")
        )
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "capture"
        self.feed = Feed()

    def open(self, supplied=None):
        return window.WindowCapture(
            self.directory,
            supplied or plan(),
            reader_factory=self.feed.reader,
            clock_ms=lambda: self.feed.now,
        )

    def read_slot(self, slot):
        return json.loads((self.directory / f"slot-{slot:04d}.json").read_text())

    def test_complete_window_is_only_complete_planned_slot_coverage(self):
        with self.open() as capture:
            for slot in range(3):
                self.feed.now = NOW + slot * 60_000
                status = capture.step(self.feed.now)
            self.assertEqual(status["status"], "COMPLETE")
            self.assertEqual(status["slot_counts"], {"USABLE": 3})
            self.assertEqual(status["attempted_gets"], 6)
            self.assertEqual(status["reserved_gets"], 6)
            self.assertTrue(status["full_planned_slot_coverage"])
            for name in (
                "continuous_market_history_verified",
                "source_authenticity_verified",
                "independence_verified",
                "strategy_validated",
                "execution_available",
            ):
                self.assertFalse(status[name])
            self.assertEqual(status["actual_orders"], 0)
        with self.open() as resumed:
            self.assertEqual(resumed.step(NOW + 3 * 60_000), status)
        self.assertEqual(len(self.feed.calls), 6)

    def test_preopen_no_get_or_budget_and_end_exclusive(self):
        with self.open() as capture:
            status = capture.step(NOW - 1)
            self.assertEqual(status["reserved_gets"], 0)
            self.assertEqual(status["slot_counts"], {"WAITING": 3})
            status = capture.step(plan()["end_ms"])
            self.assertEqual(status["slot_counts"], {"MISSED": 3})
            self.assertFalse(status["full_planned_slot_coverage"])
        self.assertEqual(self.feed.calls, [])

    def test_same_slot_duplicate_is_noop_across_restart(self):
        with self.open() as capture:
            one = capture.step(NOW)
            self.assertEqual(capture.step(NOW + 1), one)
        with self.open() as capture:
            self.assertEqual(capture.step(NOW + 2), one)
        self.assertEqual(self.feed.calls, ["market", "orderbook"])

    def test_slots_missed_are_not_backfilled(self):
        self.feed.now += 2 * 60_000
        with self.open() as capture:
            status = capture.step(self.feed.now)
            self.assertEqual(status["slot_counts"], {"MISSED": 2, "USABLE": 1})
            self.assertEqual(status["status"], "INCOMPLETE")
            self.assertEqual(status["reserved_gets"], 2)
        self.assertEqual(self.feed.calls, ["market", "orderbook"])

    def test_both_gets_reserved_before_first_get_and_no_transaction_during_http(self):
        with self.open() as capture:

            def inspect(resource):
                self.assertFalse(capture._db.in_transaction)
                with closing(sqlite3.connect(capture.path)) as other:
                    reserved, attempted = other.execute(
                        "SELECT reserved_gets,attempted_gets FROM window_meta"
                    ).fetchone()
                    status = other.execute(
                        "SELECT status FROM window_slots WHERE slot=0"
                    ).fetchone()[0]
                self.assertEqual(reserved, 2)
                self.assertEqual(attempted, 1 if resource == "market" else 2)
                self.assertEqual(status, "IN_PROGRESS")

            self.feed.before_get = inspect
            capture.step(NOW)

    def test_crash_after_get_reservation_never_retries_slot(self):
        self.feed.errors["market"] = KeyboardInterrupt()
        with self.open() as capture:
            with self.assertRaises(KeyboardInterrupt):
                capture.step(NOW)
        self.feed.errors.clear()
        with self.open() as capture:
            status = capture.step(NOW + 1)
            self.assertEqual(
                status["slot_counts"], {"INTERRUPTED_UNKNOWN": 1, "WAITING": 2}
            )
            self.assertEqual(status["reserved_gets"], 2)
            self.assertEqual(status["attempted_gets"], 1)
            self.feed.now += 60_000
            status = capture.step(self.feed.now)
            self.assertEqual(status["reserved_gets"], 4)
            self.assertFalse(status["full_planned_slot_coverage"])
        self.assertEqual(self.feed.calls, ["market", "market", "orderbook"])

    def test_crash_after_file_write_preserves_orphan_and_no_resend(self):
        with self.open() as capture:
            original = capture._save

            def crash_after_write(*args):
                original(*args)
                raise KeyboardInterrupt()

            with patch.object(capture, "_save", side_effect=crash_after_write):
                with self.assertRaises(KeyboardInterrupt):
                    capture.step(NOW)
        artifact = (self.directory / "slot-0000.json").read_bytes()
        with self.open() as capture:
            status = capture.step(NOW + 1)
            self.assertEqual(status["slot_counts"]["INTERRUPTED_UNKNOWN"], 1)
        self.assertEqual((self.directory / "slot-0000.json").read_bytes(), artifact)
        self.assertEqual(len(self.feed.calls), 2)

    def test_closed_market_stops_without_orderbook_and_burns_reserved_budget(self):
        self.feed.closed = True
        with self.open() as capture:
            status = capture.step(NOW)
            self.assertEqual(status["status"], "INCOMPLETE")
            self.assertEqual(status["reason"], "MARKET_CLOSED")
            self.assertEqual(status["slot_counts"], {"CLOSED": 1, "NOT_COLLECTED": 2})
            self.assertEqual(status["unused_reserved_gets"], 1)
            self.assertEqual(capture.step(NOW + 60_000), status)
        self.assertEqual(self.feed.calls, ["market"])
        self.assertEqual(set(self.read_slot(0)["snapshot"]["observations"]), {"market"})

    def test_invalid_book_body_is_retained_and_next_slot_can_continue(self):
        self.feed.malformed = True
        with self.open() as capture:
            status = capture.step(NOW)
            self.assertEqual(status["slot_counts"]["INVALID"], 1)
            self.assertEqual(
                self.read_slot(0)["snapshot"]["observations"]["orderbook"]["body"][
                    "orderbook"
                ]["asks"],
                [],
            )
            self.feed.malformed = False
            self.feed.now += 60_000
            self.assertEqual(capture.step(self.feed.now)["slot_counts"]["USABLE"], 1)

    def test_book_failure_keeps_market_and_never_retries(self):
        self.feed.errors["orderbook"] = DataError("PUBLIC_DATA_UNAVAILABLE")
        with self.open() as capture:
            status = capture.step(NOW)
            self.assertEqual(capture.step(NOW + 2), status)
        record = self.read_slot(0)
        self.assertEqual(record["reason"], "PUBLIC_DATA_UNAVAILABLE")
        self.assertEqual(set(record["snapshot"]["observations"]), {"market"})
        self.assertEqual(len(self.feed.calls), 2)

    def test_unknown_error_message_never_saved_or_returned(self):
        self.feed.errors["market"] = RuntimeError(
            "SECRET_TOKEN https://private.example"
        )
        with self.open() as capture:
            result = capture.step(NOW)
        raw = (self.directory / "slot-0000.json").read_text()
        self.assertEqual(self.read_slot(0)["reason"], "WINDOW_CAPTURE_FAILED")
        self.assertNotIn("SECRET_TOKEN", raw + json.dumps(result))
        self.assertNotIn("private.example", raw + json.dumps(result))

    def test_deadline_checked_again_before_book_get(self):
        self.feed.after_market = lambda: setattr(
            self.feed, "now", plan()["deadline_ms"]
        )
        with self.open() as capture:
            result = capture.step(NOW)
            self.assertEqual(result["reason"], "DEADLINE_REACHED")
            self.assertEqual(result["attempted_gets"], 1)
        self.assertEqual(self.feed.calls, ["market"])

    def test_crossed_slot_before_book_is_invalid_without_backfill(self):
        self.feed.after_market = lambda: setattr(self.feed, "now", NOW + 60_000)
        with self.open() as capture:
            status = capture.step(NOW)
            self.assertEqual(status["slot_counts"]["INVALID"], 1)
        self.assertEqual(self.read_slot(0)["reason"], "SLOT_TIME_EXPIRED")
        self.assertEqual(self.feed.calls, ["market"])

    def test_changed_metadata_invalidates_slot_before_book(self):
        with self.open() as capture:
            capture.step(NOW)
            self.feed.now += 60_000
            self.feed.metadata_version = 2
            result = capture.step(self.feed.now)
        self.assertEqual(result["slot_counts"]["INVALID"], 1)
        self.assertEqual(
            self.read_slot(1)["reason"], "INSTRUMENT_CHANGED_DURING_WINDOW"
        )
        self.assertEqual(self.feed.calls, ["market", "orderbook", "market"])

    def test_single_receiver_and_immutable_plan(self):
        with self.open() as capture:
            with self.assertRaisesRegex(DataError, "WINDOW_BUSY"):
                self.open()
            capture.step(NOW)
        altered = plan()
        altered["collection_id"] = "other"
        with self.assertRaisesRegex(DataError, "WINDOW_PLAN_CONFLICT"):
            self.open(altered)
        self.assertEqual(len(self.feed.calls), 2)

    def test_storage_exhaustion_stops_before_new_request(self):
        supplied = plan()
        supplied["max_storage_bytes"] = (
            window.INTERNAL_STORAGE_RESERVE + window.MAX_CAPTURE_BYTES
        )
        with self.open(supplied) as capture:
            capture.step(NOW)
            self.feed.now += 60_000
            result = capture.step(self.feed.now)
        self.assertEqual(result["reason"], "STORAGE_LIMIT")
        self.assertEqual(result["reserved_gets"], 2)
        self.assertEqual(len(self.feed.calls), 2)

    def test_write_failure_keeps_unknown_reserve_and_stops(self):
        with self.open() as capture:
            with patch.object(capture, "_save", side_effect=OSError("private-file")):
                result = capture.step(NOW)
            self.assertEqual(result["reason"], "LOCAL_STORAGE_ERROR")
            self.assertEqual(result["slot_counts"]["INTERRUPTED_UNKNOWN"], 1)
            self.assertEqual(result["reserved_gets"], 2)
        with self.open() as capture:
            capture.step(NOW + 60_000)
        self.assertEqual(len(self.feed.calls), 2)

    def test_full_coverage_requires_saved_files_to_match_hashes(self):
        with self.open(plan(1)) as capture:
            capture.step(NOW)
            path = self.directory / "slot-0000.json"
            raw = path.read_bytes()
            path.write_bytes(raw.replace(b'"public"', b'"PUBLIC"', 1))
            status = capture.snapshot()
            self.assertEqual(status["reason"], "EVIDENCE_INTEGRITY_FAILURE")
            self.assertFalse(status["full_planned_slot_coverage"])
            self.assertFalse(status["retained_artifacts_integrity_checked"])

    def test_partial_missing_file_stops_future_collection_durably(self):
        with self.open() as capture:
            capture.step(NOW)
        (self.directory / "slot-0000.json").unlink()
        self.feed.now += 60_000
        with self.open() as capture:
            status = capture.step(self.feed.now)
            self.assertEqual(status["reason"], "EVIDENCE_INTEGRITY_FAILURE")
            self.assertEqual(status["status"], "INCOMPLETE")
        self.assertEqual(len(self.feed.calls), 2)

    def test_invalid_metadata_never_poisons_pinned_identity(self):
        self.feed.market_changes["tick_size"] = "bogus"
        with self.open() as capture:
            first = capture.step(NOW)
            self.assertIsNone(first["instrument_metadata_sha256"])
            self.feed.market_changes.clear()
            self.feed.now += 60_000
            second = capture.step(self.feed.now)
            self.assertEqual(second["slot_counts"]["USABLE"], 1)
        self.assertEqual(self.feed.calls, ["market", "market", "orderbook"])

    def test_status_file_is_atomic_bounded_and_contains_no_bodies(self):
        with self.open() as capture:
            capture.step(NOW)
            status = capture.write_status(at_ms=NOW + 100)
            saved = json.loads((self.directory / "status.json").read_text())
            self.assertEqual(saved, status)
            self.assertEqual(saved["status_written_at_ms"], NOW + 100)
            self.assertNotIn("observations", saved)
            self.assertFalse((self.directory / "status.pending").exists())
            self.assertLess(
                (self.directory / "status.json").stat().st_size, window.MAX_PLAN_BYTES
            )
            original = (self.directory / "status.json").read_bytes()
            with patch(
                "us500_window_capture.os.replace", side_effect=OSError("INJECTED")
            ):
                with self.assertRaises(OSError):
                    capture.write_status(at_ms=NOW + 200)
            self.assertEqual((self.directory / "status.json").read_bytes(), original)
            self.assertTrue((self.directory / "status.pending").exists())
        with self.open() as capture:
            self.assertEqual(capture.write_status(at_ms=NOW + 300)["reserved_gets"], 2)

    def test_invalid_plan_no_directory_mutation(self):
        changes = {
            "environment": "demo",
            "ticker": "OTHER",
            "cadence_ms": 1000,
            "max_gets": 9999,
            "max_slots": True,
            "max_storage_bytes": 1,
            "start_ms": NOW + 1,
            "end_ms": NOW,
            "deadline_ms": NOW + 27 * 60 * 60 * 1000,
        }
        for key, value in changes.items():
            supplied = plan()
            supplied[key] = value
            with self.subTest(key=key), self.assertRaises(DataError):
                self.open(supplied)
            self.assertFalse(self.directory.exists())

    def test_foreign_directory_and_symlink_refused(self):
        self.directory.mkdir()
        historical = self.directory / "historical.db"
        historical.write_bytes(b"untouched")
        with self.assertRaisesRegex(DataError, "FOREIGN_WINDOW_DIRECTORY"):
            self.open()
        self.assertEqual(historical.read_bytes(), b"untouched")
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaisesRegex(DataError, "WINDOW_DIRECTORY_SYMLINK"):
            window.WindowCapture(link, plan())

    def test_clock_regression_stops_without_more_requests(self):
        with self.open() as capture:
            capture.step(NOW)
            result = capture.step(NOW - 1)
            self.assertEqual(result["reason"], "CLOCK_REGRESSION")
        self.assertEqual(len(self.feed.calls), 2)


if __name__ == "__main__":
    unittest.main()
