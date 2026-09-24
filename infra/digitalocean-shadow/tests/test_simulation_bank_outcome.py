"""Tests for simulation_bank.reserve_with_evidence_outcome — C1/6.2.

The bank now reports WHAT a reservation call did, read inside the same
BEGIN IMMEDIATE as the write: CREATED, REPLAY_ACTIVE or REPLAY_RELEASED. Before
this, an identical replay of a RELEASED key was a silent no-op, and a caller
that inferred "reserved" from the absence of an exception reported a released
reservation as active.

The existing `reserve` / `reserve_with_evidence` return values are pinned
unchanged: an identical replay still returns a snapshot EQUAL to the original.
No network.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import simulation_bank as bank  # noqa: E402

EVIDENCE = {"fill_id": 1, "experiment_id": "exp-001"}


class OutcomeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "sim-bank.sqlite3"
        for name in ("connect", "connect_ex"):
            blocker = patch.object(
                socket.socket, name, side_effect=AssertionError("NETWORK FORBIDDEN")
            )
            blocker.start()
            self.addCleanup(blocker.stop)
        bank.init_bank(self.db, initial_capital_usd="200.00")

    def _book(self, key="k1", amount="0.84", evidence=EVIDENCE):
        return bank.reserve_with_evidence_outcome(
            self.db,
            idempotency_key=key,
            origin="M5",
            cycle_id="c1",
            amount_usd=amount,
            evidence=evidence,
        )


class OutcomeTests(OutcomeTestCase):
    def test_first_call_creates(self):
        booked = self._book()
        self.assertEqual(booked["outcome"], "CREATED")
        self.assertIs(booked["reservation_active"], True)
        self.assertEqual(booked["snapshot"]["reserved_usd"], "0.84")

    def test_identical_replay_of_active_is_reported_as_replay(self):
        first = self._book()
        second = self._book()
        self.assertEqual(second["outcome"], "REPLAY_ACTIVE")
        self.assertIs(second["reservation_active"], True)
        self.assertEqual(second["snapshot"], first["snapshot"])

    def test_identical_replay_of_released_is_not_reactivated(self):
        self._book()
        released = bank.release(self.db, idempotency_key="k1")

        replay = self._book()

        self.assertEqual(replay["outcome"], "REPLAY_RELEASED")
        self.assertIs(replay["reservation_active"], False)
        self.assertEqual(replay["snapshot"]["reserved_usd"], "0.00")
        self.assertEqual(replay["snapshot"]["active_reservations"], [])
        self.assertEqual(replay["snapshot"]["revision"], released["revision"])

    def test_changed_replay_is_still_a_conflict(self):
        self._book()
        with self.assertRaises(bank.SimulationBankConflictError):
            self._book(amount="0.85")
        with self.assertRaises(bank.SimulationBankConflictError):
            self._book(evidence={"fill_id": 2, "experiment_id": "exp-001"})

    def test_changed_replay_of_released_is_a_conflict_too(self):
        """Released is not a free slot: the key stays bound to its original payload."""
        self._book()
        bank.release(self.db, idempotency_key="k1")
        with self.assertRaises(bank.SimulationBankConflictError):
            self._book(amount="0.85")
        self.assertEqual(bank.get_snapshot(self.db)["reserved_usd"], "0.00")

    def test_existing_reserve_with_evidence_contract_is_unchanged(self):
        """Replay still returns an EQUAL bare snapshot — callers rely on it."""
        kwargs = {"idempotency_key": "k1", "origin": "M5", "cycle_id": "c1", "amount_usd": "0.84"}
        first = bank.reserve_with_evidence(self.db, evidence=EVIDENCE, **kwargs)
        second = bank.reserve_with_evidence(self.db, evidence=EVIDENCE, **kwargs)
        self.assertEqual(first, second)
        self.assertNotIn("outcome", first)


class ConcurrencyTests(OutcomeTestCase):
    def test_concurrent_identical_calls_create_exactly_once(self):
        """Eight threads, eight connections, one key: one CREATED, seven replays, one charge."""
        outcomes, errors = [], []
        start = threading.Barrier(8)

        def worker():
            try:
                start.wait()
                outcomes.append(self._book()["outcome"])
            except Exception as exc:  # pragma: no cover - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), ["CREATED"] + ["REPLAY_ACTIVE"] * 7)
        self.assertEqual(bank.get_snapshot(self.db)["reserved_usd"], "0.84")

    def test_concurrent_replays_after_release_never_reactivate(self):
        self._book()
        bank.release(self.db, idempotency_key="k1")
        outcomes = []
        start = threading.Barrier(6)

        def worker():
            start.wait()
            outcomes.append(self._book()["outcome"])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(outcomes, ["REPLAY_RELEASED"] * 6)
        self.assertEqual(bank.get_snapshot(self.db)["reserved_usd"], "0.00")


if __name__ == "__main__":
    unittest.main()
