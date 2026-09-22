"""Tests for m5_ledger_bridge — C1/6.1 (durable-row verification) and 6.2 (reservation state).

No network: every socket entry point is patched to fail. Source and bank are two
separate temporary SQLite files. The source fixture mimics the durable
`mm_shadow_fills` shape (the 12 required columns plus unrelated extras).

What these tests pin is the DESIRED behavior, not what the candidate did:
  - a reservation answer reflects the PERSISTED state read inside the bank's own
    transaction — never "no exception, therefore RESERVED"
  - a released reservation is never reported active and never reactivated
  - invalid evidence leaves both databases untouched (no partial mutation)
  - a reservation is NOT a close and NOT a gain: releasing it returns
    availability and recognizes no P&L
"""

from __future__ import annotations

import ast
import importlib
import json
import socket
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import m5_ledger_bridge as bridge  # noqa: E402
import simulation_bank as bank  # noqa: E402

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
KEY = "m5-shadow-fill-risk-exp-001-123"

# The 12 fields the bridge compares, plus columns the real table also carries.
_SOURCE_COLUMNS = (
    "id INTEGER PRIMARY KEY, ticker TEXT, side TEXT, price_cents INTEGER, count INTEGER, "
    "fee_effective_cents INTEGER, fee_model TEXT, metric_version TEXT, experiment_id TEXT, "
    "fee_source TEXT, fee_type TEXT, fee_multiplier FLOAT, rule TEXT, created_at TEXT"
)


def _row(**overrides):
    # buy 2 @ 40¢, taker, M=1: fee = ceil(7*2*40*60/10000) = ceil(3.36) = 4¢;
    # max loss = 40*2 + 4 = 84¢.
    base = {
        "id": 123,
        "ticker": "KXTEST-26",
        "side": "buy",
        "price_cents": 40,
        "count": 2,
        "fee_effective_cents": 4,
        "fee_model": "taker",
        "metric_version": "f1-v2-bbo-depth",
        "experiment_id": "exp-001",
        "fee_source": "series",
        "fee_type": "quadratic_with_maker_fees",
        "fee_multiplier": 1.0,
        "rule": "ask 38 < bid 40",
        "created_at": "2026-09-22 11:59:00",
    }
    base.update(overrides)
    return base


def _payload(**fill_overrides):
    fill = {
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
    fill.update(fill_overrides)
    return {"schema_version": "botkalshi-m5-fill-input-v1", "fill": fill}


class LedgerBridgeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.source = self.dir / "trades.db"
        self.bank_path = self.dir / "sim-bank.sqlite3"
        for name in ("connect", "connect_ex"):
            blocker = patch.object(
                socket.socket, name, side_effect=AssertionError("NETWORK FORBIDDEN")
            )
            blocker.start()
            self.addCleanup(blocker.stop)
        blocker = patch("socket.create_connection", side_effect=AssertionError("NETWORK FORBIDDEN"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def _make_source(self, rows=None, *, columns=_SOURCE_COLUMNS, path=None):
        path = path or self.source
        con = sqlite3.connect(path)
        try:
            con.execute(f"CREATE TABLE mm_shadow_fills ({columns})")
            for row in rows if rows is not None else [_row()]:
                cols = ",".join(row)
                marks = ",".join("?" for _ in row)
                con.execute(
                    f"INSERT INTO mm_shadow_fills ({cols}) VALUES ({marks})", tuple(row.values())
                )
            con.commit()
        finally:
            con.close()
        return path

    def _update_source(self, **values):
        con = sqlite3.connect(self.source)
        try:
            sets = ",".join(f"{k}=?" for k in values)
            con.execute(f"UPDATE mm_shadow_fills SET {sets} WHERE id=123", tuple(values.values()))
            con.commit()
        finally:
            con.close()

    def _init_bank(self, capital="200.00"):
        bank.init_bank(self.bank_path, initial_capital_usd=capital)

    def _apply(self, payload=None, *, source=None, bank_path=None):
        return bridge.apply_ledger_verified_m5_fill(
            source or self.source, bank_path or self.bank_path, payload or _payload(), now=NOW
        )

    def assert_bank_untouched(self, before):
        after = bank.get_snapshot(self.bank_path)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["reserved_usd"], before["reserved_usd"])
        self.assertEqual(after["active_reservations"], before["active_reservations"])


# --------------------------------------------------------------------------- 6.1


class DurableRowVerificationTests(LedgerBridgeTestCase):
    def test_matching_row_reserves_with_verified_evidence(self):
        self._make_source()
        self._init_bank()

        result = self._apply()

        self.assertEqual(result["status"], "RESERVED", result["reason_codes"])
        self.assertEqual(result["evidence_state"], "LEDGER_VERIFIED_SHADOW_FILL")
        self.assertEqual(result["reservation_outcome"], "CREATED")
        self.assertIs(result["reservation_active"], True)
        snap = result["reservation"]
        self.assertEqual(snap["reserved_usd"], "0.84")
        evidence = snap["active_reservations"][0]["evidence"]
        self.assertEqual(
            evidence["source_record"],
            {"table": "mm_shadow_fills", "experiment_id": "exp-001", "id": 123},
        )
        self.assertEqual(evidence["exactness"], "EXACT_CONDITIONAL_ON_VERIFIED_LOCAL_SHADOW_ROW")
        # No absolute path and no timestamp enter the idempotent stored evidence.
        stored = json.dumps(evidence)
        self.assertNotIn(str(self.dir), stored)
        self.assertNotIn("2026-09-22", stored)

    def test_result_never_claims_execution_or_real_eligibility(self):
        self._make_source()
        self._init_bank()
        result = self._apply()
        self.assertEqual(result["mode"], "SIMULATION_ONLY")
        self.assertEqual(result["capital_source"], "FICTIONAL_TEST_CAPITAL")
        self.assertIs(result["execution_authorized"], False)
        self.assertIs(result["order_capability_present"], False)
        self.assertIs(result["real_entry_eligible"], False)

    def test_int_and_float_multiplier_are_the_same_fact(self):
        """SQLite hands back 1.0 for a FLOAT column; the declaration says 1. Same value."""
        self._make_source([_row(fee_multiplier=1.0)])
        self._init_bank()
        self.assertEqual(self._apply(_payload(fee_multiplier=1))["status"], "RESERVED")

    def test_identity_is_experiment_plus_id_not_id_alone(self):
        """Same numeric id under ANOTHER cohort is not this fill."""
        self._make_source([_row(experiment_id="exp-OTRA")])
        self._init_bank()
        before = bank.get_snapshot(self.bank_path)

        result = self._apply()

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["SOURCE_ROW_MISSING_OR_DUPLICATED"])
        self.assertIsNone(result["reservation"])
        self.assert_bank_untouched(before)

    def test_contradicting_row_blocks_without_mutation(self):
        """Fields that keep the fee reconciled but contradict the declaration."""
        for overrides in (
            {"ticker": "KXOTRO-26"},
            {"side": "sell"},
            {"fee_source": "event_override"},
        ):
            with self.subTest(overrides=overrides):
                self.setUp()
                self._make_source([_row(**overrides)])
                self._init_bank()
                before = bank.get_snapshot(self.bank_path)

                result = self._apply()

                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["reason_codes"], ["SOURCE_DECLARATION_MISMATCH"])
                self.assert_bank_untouched(before)

    def test_inconsistent_recorded_fee_blocks(self):
        """A durable fee that does not reconcile is not usable evidence, whatever the declaration says."""
        self._make_source([_row(fee_effective_cents=5)])
        self._init_bank()
        before = bank.get_snapshot(self.bank_path)

        result = self._apply(_payload(fee_effective_cents=4))

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["INVALID_DECLARATION"])
        self.assert_bank_untouched(before)

    def test_missing_source_is_not_created(self):
        self._init_bank()
        result = self._apply()
        self.assertEqual(result["reason_codes"], ["SOURCE_NOT_FOUND"])
        self.assertFalse(self.source.exists(), "a missing source must never be created empty")

    def test_source_and_bank_must_be_different_files(self):
        self._init_bank()
        result = self._apply(source=self.bank_path)
        self.assertEqual(result["reason_codes"], ["SOURCE_BANK_SAME_FILE"])

    def test_incomplete_schema_blocks(self):
        self._make_source(
            [{k: v for k, v in _row().items() if k != "fee_source"}],
            columns=_SOURCE_COLUMNS.replace("fee_source TEXT, ", ""),
        )
        self._init_bank()
        self.assertEqual(self._apply()["reason_codes"], ["SOURCE_SCHEMA_INCOMPLETE"])

    def test_a_view_is_not_the_table_of_record(self):
        other = self.dir / "other.db"
        self._make_source(path=other)
        con = sqlite3.connect(self.source)
        try:
            con.execute(f"ATTACH DATABASE '{other}' AS o")
            con.execute("CREATE TABLE real_fills AS SELECT * FROM o.mm_shadow_fills")
            con.execute("CREATE VIEW mm_shadow_fills AS SELECT * FROM real_fills")
            con.commit()
        finally:
            con.close()
        self._init_bank()
        self.assertEqual(self._apply()["reason_codes"], ["SOURCE_TABLE_REQUIRED"])

    def test_duplicated_identity_blocks(self):
        self._make_source([_row(), _row()], columns=_SOURCE_COLUMNS.replace(" PRIMARY KEY", ""))
        self._init_bank()
        self.assertEqual(self._apply()["reason_codes"], ["SOURCE_ROW_MISSING_OR_DUPLICATED"])

    def test_reading_does_not_modify_the_source(self):
        self._make_source()
        self._init_bank()
        before = self.source.read_bytes()
        self._apply()
        self.assertEqual(self.source.read_bytes(), before)

    def test_symlinked_source_is_rejected(self):
        real = self._make_source(path=self.dir / "real.db")
        self.source.symlink_to(real)
        self._init_bank()
        self.assertEqual(self._apply()["reason_codes"], ["SYMLINK_PATH"])


# --------------------------------------------------------------------------- 6.2


class ReservationStateTests(LedgerBridgeTestCase):
    def setUp(self):
        super().setUp()
        self._make_source()
        self._init_bank()

    def test_active_replay_is_reported_as_replay_and_does_not_double(self):
        first = self._apply()
        second = self._apply()

        self.assertEqual(first["status"], "RESERVED")
        self.assertEqual(second["status"], "ALREADY_RESERVED")
        self.assertEqual(second["reservation_outcome"], "REPLAY_ACTIVE")
        self.assertIs(second["reservation_active"], True)
        self.assertEqual(second["reservation"]["reserved_usd"], "0.84")
        self.assertEqual(second["reservation"]["revision"], first["reservation"]["revision"])

    def test_reserve_release_replay_restart_replay(self):
        """The mandatory sequence: a released reservation is never reported active,
        never reactivated, and no step adds risk or capital — across a restart."""
        global bank, bridge
        reserved = self._apply()
        self.assertEqual(reserved["status"], "RESERVED")

        released = bank.release(self.bank_path, idempotency_key=KEY)
        self.assertEqual(released["reserved_usd"], "0.00")

        replay = self._apply()
        self.assertEqual(replay["status"], "ALREADY_RELEASED")
        self.assertEqual(replay["reservation_outcome"], "REPLAY_RELEASED")
        self.assertIs(replay["reservation_active"], False)
        self.assertEqual(replay["reservation"]["reserved_usd"], "0.00")
        self.assertEqual(replay["reservation"]["active_reservations"], [])
        self.assertEqual(replay["reservation"]["revision"], released["revision"])

        # Restart: fresh module objects, nothing carried in memory; state is only on disk.
        bank = importlib.reload(bank)
        bridge = importlib.reload(bridge)

        after_restart = self._apply()
        self.assertEqual(after_restart["status"], "ALREADY_RELEASED")
        self.assertIs(after_restart["reservation_active"], False)
        self.assertEqual(after_restart["reservation"]["reserved_usd"], "0.00")
        self.assertEqual(after_restart["reservation"]["available_usd"], "200.00")
        self.assertEqual(after_restart["reservation"]["revision"], released["revision"])

    def test_release_is_not_a_gain(self):
        """Liberar no es ganar: capital stays the initial fictional capital; no P&L appears."""
        self._apply()
        released = bank.release(self.bank_path, idempotency_key=KEY)
        self.assertEqual(released["initial_capital_usd"], "200.00")
        self.assertEqual(released["available_usd"], "200.00")
        replay = self._apply()
        for key in replay:
            self.assertNotIn("pnl", key.lower())
            self.assertNotIn("profit", key.lower())

    def test_changed_replay_is_a_conflict_not_a_reconciliation(self):
        """A producer that mutated risk fields after acceptance gets a conflict."""
        self._apply()
        before = bank.get_snapshot(self.bank_path)
        self._update_source(fee_source="event_override")

        result = self._apply(_payload(fee_source="event_override"))

        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["BANK_REJECTED", "SimulationBankConflictError"])
        self.assert_bank_untouched(before)

    def test_insufficient_fictional_capital_blocks_without_mutation(self):
        self.bank_path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.bank_path) + suffix).unlink(missing_ok=True)
        self._init_bank(capital="0.50")
        before = bank.get_snapshot(self.bank_path)

        result = self._apply()

        self.assertEqual(
            result["reason_codes"], ["BANK_REJECTED", "SimulationBankInsufficientFundsError"]
        )
        self.assert_bank_untouched(before)


class BankPathTests(LedgerBridgeTestCase):
    def test_missing_bank_is_not_created(self):
        """A rejection must not leave a new, empty bank file behind."""
        self._make_source()
        result = self._apply()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason_codes"], ["BANK_NOT_FOUND"])
        self.assertFalse(self.bank_path.exists())

    def test_uninitialized_bank_blocks(self):
        self._make_source()
        sqlite3.connect(self.bank_path).close()
        result = self._apply()
        self.assertEqual(
            result["reason_codes"], ["BANK_REJECTED", "SimulationBankNotInitializedError"]
        )


class ModuleIsolationTests(unittest.TestCase):
    def test_module_imports_nothing_that_can_reach_an_account_or_the_network(self):
        tree = ast.parse((BASE / "m5_ledger_bridge.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        allowed = {
            "__future__",
            "contextlib",
            "datetime",
            "fractions",
            "pathlib",
            "math",
            "re",
            "sqlite3",
            "typing",
            "simulation_bank",
            "m5_shadow_fill_review",
        }
        self.assertLessEqual(imported, allowed, imported - allowed)


if __name__ == "__main__":
    unittest.main()
