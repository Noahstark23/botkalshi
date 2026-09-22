"""Tests for the SIMULATION_ONLY shared capital ledger. No network, account or host."""

from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import simulation_bank as bank  # noqa: E402


class SimulationBankTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "sim-bank.sqlite3"
        for name in ("connect", "connect_ex"):
            blocker = patch.object(socket.socket, name, side_effect=AssertionError("NETWORK FORBIDDEN"))
            blocker.start()
            self.addCleanup(blocker.stop)
        blocker = patch("socket.create_connection", side_effect=AssertionError("NETWORK FORBIDDEN"))
        blocker.start()
        self.addCleanup(blocker.stop)


class ModuleImportTests(unittest.TestCase):
    def test_import_does_not_touch_disk_or_network(self):
        import importlib
        import sqlite3

        with (
            patch.object(socket.socket, "connect", side_effect=AssertionError("NETWORK FORBIDDEN")),
            patch.object(sqlite3, "connect", side_effect=AssertionError("DB FORBIDDEN")),
        ):
            importlib.reload(bank)

    def test_module_has_no_network_imports(self):
        import ast

        tree = ast.parse((BASE / "simulation_bank.py").read_text())
        allowed = {"__future__", "contextlib", "datetime", "pathlib", "re", "sqlite3", "typing"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertIn(node.module.split(".")[0], allowed)


class InitBankTests(SimulationBankTestCase):
    def test_init_creates_bank_with_explicit_capital(self):
        snap = bank.init_bank(self.db_path, initial_capital_usd="100.00")
        self.assertEqual(snap["initial_capital_usd"], "100.00")
        self.assertEqual(snap["reserved_usd"], "0.00")
        self.assertEqual(snap["available_usd"], "100.00")
        self.assertEqual(snap["revision"], 1)
        self.assertEqual(snap["mode"], "SIMULATION_ONLY")
        self.assertEqual(snap["capital_source"], "FICTIONAL_TEST_CAPITAL")
        self.assertFalse(snap["execution_authorized"])
        self.assertFalse(snap["order_capability_present"])
        self.assertFalse(snap["real_entry_eligible"])
        self.assertEqual(snap["active_reservations"], [])

    def test_repeated_init_same_capital_is_idempotent(self):
        first = bank.init_bank(self.db_path, initial_capital_usd="100.00")
        second = bank.init_bank(self.db_path, initial_capital_usd="100.00")
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(second["initial_capital_usd"], "100.00")

    def test_repeated_init_different_capital_blocks(self):
        bank.init_bank(self.db_path, initial_capital_usd="100.00")
        with self.assertRaises(bank.SimulationBankConflictError):
            bank.init_bank(self.db_path, initial_capital_usd="150.00")
        # Capital and revision are unchanged after the blocked attempt.
        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(snap["initial_capital_usd"], "100.00")
        self.assertEqual(snap["revision"], 1)

    def test_invalid_capital_blocks(self):
        for bad in ("0.00", "-5.00", "abc", "5.001", "NaN", "inf", float("nan"), float("inf"), 100, 100.0, None, "1" * 40):
            with self.assertRaises(bank.SimulationBankError):
                bank.init_bank(self.db_path, initial_capital_usd=bad)
        with self.assertRaises(bank.SimulationBankNotInitializedError):
            bank.get_snapshot(self.db_path)


class ReserveTests(SimulationBankTestCase):
    def setUp(self):
        super().setUp()
        bank.init_bank(self.db_path, initial_capital_usd="100.00")

    def test_reserve_before_init_blocks(self):
        fresh = Path(self._tmp.name) / "uninitialized.sqlite3"
        with self.assertRaises(bank.SimulationBankNotInitializedError):
            bank.reserve(
                fresh, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="1.00"
            )

    def test_reserve_reduces_available_and_bumps_revision(self):
        before = bank.get_snapshot(self.db_path)
        snap = bank.reserve(
            self.db_path, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="30.00"
        )
        self.assertEqual(snap["reserved_usd"], "30.00")
        self.assertEqual(snap["available_usd"], "70.00")
        self.assertEqual(snap["revision"], before["revision"] + 1)
        self.assertEqual(len(snap["active_reservations"]), 1)
        self.assertEqual(snap["active_reservations"][0]["idempotency_key"], "k1")
        self.assertEqual(snap["active_reservations"][0]["origin"], "M1")
        self.assertEqual(snap["active_reservations"][0]["cycle_id"], "c1")
        self.assertEqual(snap["active_reservations"][0]["amount_usd"], "30.00")

    def test_identical_replay_does_not_double_charge_or_bump_revision(self):
        first = bank.reserve(
            self.db_path, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="30.00"
        )
        second = bank.reserve(
            self.db_path, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="30.00"
        )
        self.assertEqual(first, second)
        self.assertEqual(second["reserved_usd"], "30.00")
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(len(second["active_reservations"]), 1)

    def test_conflicting_replay_same_key_blocks(self):
        bank.reserve(
            self.db_path, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="30.00"
        )
        with self.assertRaises(bank.SimulationBankConflictError):
            bank.reserve(
                self.db_path, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="31.00"
            )
        with self.assertRaises(bank.SimulationBankConflictError):
            bank.reserve(
                self.db_path, idempotency_key="k1", origin="M5", cycle_id="c1", amount_usd="30.00"
            )
        with self.assertRaises(bank.SimulationBankConflictError):
            bank.reserve(
                self.db_path, idempotency_key="k1", origin="M1", cycle_id="c2", amount_usd="30.00"
            )
        # Blocked replays never changed the original reservation.
        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(snap["reserved_usd"], "30.00")

    def test_reservation_exceeding_capital_blocks_and_reserves_nothing(self):
        with self.assertRaises(bank.SimulationBankInsufficientFundsError):
            bank.reserve(
                self.db_path, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="150.00"
            )
        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(snap["reserved_usd"], "0.00")
        self.assertEqual(snap["available_usd"], "100.00")
        self.assertEqual(snap["active_reservations"], [])

    def test_second_reservation_exceeding_remaining_available_blocks(self):
        bank.reserve(
            self.db_path, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="70.00"
        )
        with self.assertRaises(bank.SimulationBankInsufficientFundsError):
            bank.reserve(
                self.db_path, idempotency_key="k2", origin="M1", cycle_id="c2", amount_usd="31.00"
            )
        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(snap["reserved_usd"], "70.00")

    def test_invalid_reservation_fields_block(self):
        bad_amounts = ("0.00", "-1.00", "abc", "1.001", "NaN", "inf", float("nan"), 5, 5.0, "9" * 40)
        for bad in bad_amounts:
            with self.assertRaises(bank.SimulationBankError):
                bank.reserve(
                    self.db_path, idempotency_key="bad", origin="M1", cycle_id="c1", amount_usd=bad
                )
        for field in ("idempotency_key", "origin", "cycle_id"):
            kwargs = {
                "idempotency_key": "ok-key",
                "origin": "M1",
                "cycle_id": "c1",
                "amount_usd": "1.00",
            }
            kwargs[field] = ""
            with self.assertRaises(bank.SimulationBankValidationError):
                bank.reserve(self.db_path, **kwargs)
        # None of the blocked attempts left a trace.
        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(snap["reserved_usd"], "0.00")


class ReleaseTests(SimulationBankTestCase):
    def setUp(self):
        super().setUp()
        bank.init_bank(self.db_path, initial_capital_usd="100.00")
        bank.reserve(
            self.db_path, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="30.00"
        )

    def test_release_frees_reservation_and_bumps_revision(self):
        before = bank.get_snapshot(self.db_path)
        snap = bank.release(self.db_path, idempotency_key="k1")
        self.assertEqual(snap["reserved_usd"], "0.00")
        self.assertEqual(snap["available_usd"], "100.00")
        self.assertEqual(snap["active_reservations"], [])
        self.assertEqual(snap["revision"], before["revision"] + 1)

    def test_repeated_release_is_idempotent_and_does_not_manufacture_balance(self):
        first = bank.release(self.db_path, idempotency_key="k1")
        second = bank.release(self.db_path, idempotency_key="k1")
        self.assertEqual(first, second)
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(second["available_usd"], "100.00")
        # available never exceeds initial capital even after repeated release.
        self.assertLessEqual(_cents(second["available_usd"]), _cents(second["initial_capital_usd"]))

    def test_release_unknown_key_blocks(self):
        with self.assertRaises(bank.SimulationBankNotFoundError):
            bank.release(self.db_path, idempotency_key="never-reserved")

    def test_release_before_init_blocks(self):
        fresh = Path(self._tmp.name) / "uninitialized-release.sqlite3"
        with self.assertRaises(bank.SimulationBankNotInitializedError):
            bank.release(fresh, idempotency_key="k1")


class RestartPersistenceTests(SimulationBankTestCase):
    def test_state_survives_close_and_reopen(self):
        bank.init_bank(self.db_path, initial_capital_usd="100.00")
        bank.reserve(
            self.db_path, idempotency_key="k1", origin="M1", cycle_id="c1", amount_usd="30.00"
        )
        before = bank.get_snapshot(self.db_path)

        # Every call already opens and closes its own connection (no held handle to
        # leak), so "restart" is just calling again against the same file path.
        after = bank.get_snapshot(self.db_path)
        self.assertEqual(before, after)
        self.assertEqual(after["initial_capital_usd"], "100.00")
        self.assertEqual(after["reserved_usd"], "30.00")
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(len(after["active_reservations"]), 1)


class ConcurrencyTests(SimulationBankTestCase):
    def test_concurrent_reservations_never_overallocate(self):
        # Capital 10.00, three concurrent 4.00 reservations: only 2 can fit (8.00 <=
        # 10.00); the third must be rejected. BEGIN IMMEDIATE serializes the writers so
        # the outcome is deterministic regardless of thread interleaving.
        bank.init_bank(self.db_path, initial_capital_usd="10.00")
        barrier = threading.Barrier(3)
        results: list[tuple[str, Exception | None]] = [None] * 3  # type: ignore[list-item]

        def worker(index: int) -> None:
            barrier.wait(timeout=5)
            try:
                bank.reserve(
                    self.db_path,
                    idempotency_key=f"k{index}",
                    origin="M1",
                    cycle_id=f"c{index}",
                    amount_usd="4.00",
                )
                results[index] = ("ok", None)
            except bank.SimulationBankInsufficientFundsError as exc:
                results[index] = ("blocked", exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        outcomes = [status for status, _ in results]
        self.assertEqual(outcomes.count("ok"), 2)
        self.assertEqual(outcomes.count("blocked"), 1)

        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(snap["reserved_usd"], "8.00")
        self.assertEqual(snap["available_usd"], "2.00")
        self.assertEqual(len(snap["active_reservations"]), 2)
        self.assertLessEqual(_cents(snap["reserved_usd"]), _cents(snap["initial_capital_usd"]))

    def test_concurrent_identical_replays_settle_on_one_reservation(self):
        bank.init_bank(self.db_path, initial_capital_usd="10.00")
        barrier = threading.Barrier(4)
        errors: list[Exception] = []

        def worker() -> None:
            barrier.wait(timeout=5)
            try:
                bank.reserve(
                    self.db_path,
                    idempotency_key="shared-key",
                    origin="M1",
                    cycle_id="c1",
                    amount_usd="4.00",
                )
            except Exception as exc:  # noqa: BLE001 - captured for assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(len(snap["active_reservations"]), 1)
        self.assertEqual(snap["reserved_usd"], "4.00")


class PathSafetyTests(SimulationBankTestCase):
    def test_db_path_as_symlink_fails_closed(self):
        target = Path(self._tmp.name) / "real.sqlite3"
        link = Path(self._tmp.name) / "link.sqlite3"
        link.symlink_to(target)
        with self.assertRaises(bank.SimulationBankValidationError):
            bank.init_bank(link, initial_capital_usd="100.00")

    def test_dangling_symlink_fails_closed(self):
        # A symlink to a target that does not (yet) exist must still be rejected:
        # Path.exists() would follow the link and report False, missing the attack.
        link = Path(self._tmp.name) / "dangling.sqlite3"
        link.symlink_to(Path(self._tmp.name) / "does-not-exist.sqlite3")
        with self.assertRaises(bank.SimulationBankValidationError):
            bank.init_bank(link, initial_capital_usd="100.00")

    def test_symlinked_parent_directory_fails_closed(self):
        real_dir = Path(self._tmp.name) / "real_dir"
        real_dir.mkdir()
        link_dir = Path(self._tmp.name) / "link_dir"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        with self.assertRaises(bank.SimulationBankValidationError):
            bank.init_bank(link_dir / "sim-bank.sqlite3", initial_capital_usd="100.00")


def _cents(usd: str) -> int:
    whole, _, frac = usd.partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


if __name__ == "__main__":
    unittest.main()
