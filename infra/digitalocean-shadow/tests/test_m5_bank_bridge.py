"""Unit tests for m5_bank_bridge.apply_m5_fill. No network beyond local sqlite."""

from __future__ import annotations

import ast
import socket
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import m5_bank_bridge as bridge  # noqa: E402
import simulation_bank as bank  # noqa: E402

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)


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
        "metric_version": "f1-v2-bbo-depth",
        "fee_source": "series",
        "fee_type": "quadratic_with_maker_fees",
        "fee_multiplier": 1,
    }
    base.update(overrides)
    return base


def _payload(**fill_overrides):
    return {"schema_version": "botkalshi-m5-fill-input-v1", "fill": _fill(**fill_overrides)}


class BridgeTestCase(unittest.TestCase):
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


class AcceptedFillReservesTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        bank.init_bank(self.db_path, initial_capital_usd="100.00")

    def test_accepted_fill_reserves_exact_evidence(self):
        result = bridge.apply_m5_fill(self.db_path, _payload(), now=NOW)
        self.assertEqual(result["review"]["status"], "ACCEPTED")
        self.assertIsNone(result["reservation_error"])
        reservation = result["reservation"]
        self.assertIsNotNone(reservation)
        self.assertEqual(reservation["reserved_usd"], "0.84")
        self.assertEqual(reservation["available_usd"], "99.16")
        self.assertEqual(len(reservation["active_reservations"]), 1)
        active = reservation["active_reservations"][0]
        self.assertEqual(active["idempotency_key"], "m5-shadow-fill-risk-exp-001-123")
        self.assertEqual(active["origin"], "M5")
        self.assertEqual(active["cycle_id"], "m5-shadow-fill-exp-001-123")
        self.assertEqual(active["evidence"], result["review"]["evidence"])
        self.assertEqual(result["mode"], "SIMULATION_ONLY")
        self.assertEqual(result["capital_source"], "FICTIONAL_TEST_CAPITAL")
        self.assertEqual(result["evidence_state"], "DECLARED_FILL_NOT_LEDGER_VERIFIED")
        for field in ("execution_authorized", "order_capability_present", "real_entry_eligible"):
            self.assertFalse(result[field])


class BlockedReviewNoMutationTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        bank.init_bank(self.db_path, initial_capital_usd="100.00")

    def test_blocked_review_never_touches_the_bank(self):
        before = bank.get_snapshot(self.db_path)
        result = bridge.apply_m5_fill(self.db_path, _payload(fee_effective_cents=0), now=NOW)
        self.assertEqual(result["review"]["status"], "BLOCKED")
        self.assertIsNone(result["reservation"])
        self.assertIsNone(result["reservation_error"])
        after = bank.get_snapshot(self.db_path)
        self.assertEqual(before, after)
        self.assertEqual(after["reserved_usd"], "0.00")
        self.assertEqual(after["active_reservations"], [])

    def test_missing_field_blocked_review_never_touches_the_bank(self):
        fill = _fill()
        del fill["ticker"]
        before = bank.get_snapshot(self.db_path)
        result = bridge.apply_m5_fill(
            self.db_path, {"schema_version": "botkalshi-m5-fill-input-v1", "fill": fill}, now=NOW
        )
        self.assertIsNone(result["reservation"])
        after = bank.get_snapshot(self.db_path)
        self.assertEqual(before, after)


class InsufficientCapitalNoPartialTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        # Bank capital (0.50) is smaller than this fill's max_loss_usd (0.84).
        bank.init_bank(self.db_path, initial_capital_usd="0.50")

    def test_insufficient_capital_reports_error_and_reserves_nothing(self):
        result = bridge.apply_m5_fill(self.db_path, _payload(), now=NOW)
        self.assertEqual(result["review"]["status"], "ACCEPTED")
        self.assertIsNone(result["reservation"])
        self.assertIn("SimulationBankInsufficientFundsError", result["reservation_error"])
        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(snap["reserved_usd"], "0.00")
        self.assertEqual(snap["available_usd"], "0.50")
        self.assertEqual(snap["active_reservations"], [])


class ReplayTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        bank.init_bank(self.db_path, initial_capital_usd="100.00")

    def test_identical_replay_does_not_double_charge(self):
        first = bridge.apply_m5_fill(self.db_path, _payload(), now=NOW)
        second = bridge.apply_m5_fill(self.db_path, _payload(), now=NOW)
        self.assertEqual(first["reservation"], second["reservation"])
        self.assertEqual(second["reservation"]["reserved_usd"], "0.84")
        self.assertEqual(len(second["reservation"]["active_reservations"]), 1)


class ConflictTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        bank.init_bank(self.db_path, initial_capital_usd="100.00")

    def test_same_identity_different_evidence_conflicts_without_mutating_original(self):
        first = bridge.apply_m5_fill(self.db_path, _payload(), now=NOW)
        self.assertIsNotNone(first["reservation"])
        # Same id + experiment_id (same derived idempotency_key) but a different,
        # independently-valid declared fill: the bank must refuse to silently
        # overwrite the first reservation's evidence.
        conflicting = bridge.apply_m5_fill(
            self.db_path,
            # 7*2*60*40 is the same numerator as 7*2*40*60 (symmetric), so the
            # recomputed fee is still exactly 4 — only price_cents/max_loss_usd
            # differ, which is what makes this a genuine payload conflict.
            _payload(price_cents=60, fee_effective_cents=4),
            now=NOW,
        )
        self.assertEqual(conflicting["review"]["status"], "ACCEPTED")
        self.assertIsNone(conflicting["reservation"])
        self.assertIn("SimulationBankConflictError", conflicting["reservation_error"])
        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(snap["reserved_usd"], "0.84")
        self.assertEqual(len(snap["active_reservations"]), 1)


class RestartPersistenceTests(BridgeTestCase):
    def test_reservation_survives_close_and_reopen(self):
        bank.init_bank(self.db_path, initial_capital_usd="100.00")
        bridge.apply_m5_fill(self.db_path, _payload(), now=NOW)
        # Every bridge/bank call opens and closes its own connection, so "restart"
        # is simply calling again against the same file path.
        snap = bank.get_snapshot(self.db_path)
        self.assertEqual(snap["reserved_usd"], "0.84")
        self.assertEqual(len(snap["active_reservations"]), 1)
        self.assertEqual(snap["active_reservations"][0]["cycle_id"], "m5-shadow-fill-exp-001-123")


class NoForbiddenImportTests(unittest.TestCase):
    def test_module_imports_are_restricted_to_review_and_bank(self):
        tree = ast.parse((BASE / "m5_bank_bridge.py").read_text())
        allowed_modules = {"__future__", "datetime", "pathlib", "typing"}
        allowed_names = {"m5_shadow_fill_review", "simulation_bank"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed_modules | allowed_names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                self.assertIn(top, allowed_modules | allowed_names)


if __name__ == "__main__":
    unittest.main()
