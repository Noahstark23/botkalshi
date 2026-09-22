"""C1/6.4-6.5 — accounting close of SIMULATED positions. Oracles, not operations.

Every case starts from an independent FICTIONAL bank of USD 200.00. The 0.01 fees are
SYNTHETIC fixture parameters of the accounting test — not quotes, not Kalshi tariffs;
the ledger never recomputes fees (that is the fill reviewer's job, tested elsewhere).

What is pinned is the DESIRED behavior of the one-source-of-truth ledger:
  - realized P&L recognizes each fee ONCE and a close exactly once
  - releasing a reservation changes availability, never realized P&L ("liberar no es ganar")
  - replay of an identical event changes nothing; a changed replay is a conflict
  - a restart re-derives exactly the uninterrupted result (the projection is not stored)
  - invalid evidence and failures mid-write leave no partial mutation
  - an open position has no guessed value: its valuation is UNKNOWN
No network.
"""

from __future__ import annotations

import importlib
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import simulation_bank as bank  # noqa: E402

T = "KXTEST-26"


class LedgerTestCase(unittest.TestCase):
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

    # -- helpers -------------------------------------------------------------------
    def reserve(self, key, amount, origin="M5"):
        return bank.reserve(
            self.db, idempotency_key=key, origin=origin, cycle_id=f"c-{key}", amount_usd=amount
        )

    def fill(self, key, side, price, count=1, fee=1, *, origin="M5", position=T, reservation=None):
        return bank.record_fill(
            self.db,
            event_key=key,
            origin=origin,
            position_key=position,
            side=side,
            price_cents=price,
            count=count,
            fee_cents=fee,
            reservation_key=reservation,
        )

    def settle(self, key, payout, *, origin="M5", position=T):
        return bank.record_settlement(
            self.db, event_key=key, origin=origin, position_key=position, payout_cents=payout
        )

    def snap(self):
        return bank.get_snapshot(self.db)

    def event_count(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT COUNT(*) FROM simulation_events").fetchone()[0]
        finally:
            con.close()

    def assert_unchanged(self, before):
        after = self.snap()
        for field in (
            "revision",
            "realized_pnl_usd",
            "capital_usd",
            "reserved_usd",
            "available_usd",
            "positions",
            "active_reservations",
        ):
            self.assertEqual(after[field], before[field], field)

    def position(self, snapshot, origin="M5", key=T):
        matches = [
            p for p in snapshot["positions"] if (p["origin"], p["position_key"]) == (origin, key)
        ]
        self.assertEqual(len(matches), 1, snapshot["positions"])
        return matches[0]


# ------------------------------------------------------------------ the four oracles


class OracleTests(LedgerTestCase):
    def _open_long(self):
        # Buy 1 YES at 0.40 with fee 0.01; its worst-case loss 0.41 was reserved first.
        self.reserve("r1", "0.41")
        self.fill("f1", "buy", 40, reservation="r1")

    def test_gain(self):
        self._open_long()
        result = self.fill("f2", "sell", 52)

        s = result["snapshot"]
        self.assertEqual(result["outcome"], "RECORDED")
        self.assertEqual(s["realized_pnl_usd"], "0.10")
        self.assertEqual(s["capital_usd"], "200.10")
        self.assertEqual(self.position(s)["net_contracts"], 0)
        self.assertEqual(s["reserved_usd"], "0.00")
        self.assertEqual(s["available_usd"], "200.10")

    def test_loss(self):
        self._open_long()
        s = self.fill("f2", "sell", 35)["snapshot"]
        self.assertEqual(s["realized_pnl_usd"], "-0.07")
        self.assertEqual(s["capital_usd"], "199.93")
        self.assertEqual(self.position(s)["net_contracts"], 0)
        self.assertEqual(s["reserved_usd"], "0.00")

    def test_positive_settlement(self):
        self._open_long()
        s = self.settle("s1", 100)["snapshot"]
        self.assertEqual(s["realized_pnl_usd"], "0.59")
        self.assertEqual(s["capital_usd"], "200.59")
        pos = self.position(s)
        self.assertEqual((pos["net_contracts"], pos["settled"], pos["open_lots"]), (0, True, []))
        self.assertEqual(s["reserved_usd"], "0.00")

    def test_negative_settlement(self):
        self._open_long()
        s = self.settle("s1", 0)["snapshot"]
        self.assertEqual(s["realized_pnl_usd"], "-0.41")
        self.assertEqual(s["capital_usd"], "199.59")
        self.assertEqual(self.position(s)["settled"], True)
        self.assertEqual(s["reserved_usd"], "0.00")


class ReplayAndRestartTests(LedgerTestCase):
    def test_replaying_every_event_changes_nothing(self):
        self.reserve("r1", "0.41")
        self.fill("f1", "buy", 40, reservation="r1")
        self.fill("f2", "sell", 52)
        before = self.snap()

        for replay in (
            lambda: self.fill("f1", "buy", 40, reservation="r1"),
            lambda: self.fill("f2", "sell", 52),
        ):
            self.assertEqual(replay()["outcome"], "REPLAY")
        self.assert_unchanged(before)
        self.assertEqual(self.event_count(), 2)

    def test_replay_after_settlement_changes_nothing(self):
        self.reserve("r1", "0.41")
        self.fill("f1", "buy", 40, reservation="r1")
        self.settle("s1", 100)
        before = self.snap()
        self.assertEqual(self.settle("s1", 100)["outcome"], "REPLAY")
        self.assertEqual(self.fill("f1", "buy", 40, reservation="r1")["outcome"], "REPLAY")
        self.assert_unchanged(before)

    def test_restart_between_every_step_equals_uninterrupted_run(self):
        """Nothing is carried in memory: the projection is re-derived from disk."""
        global bank
        steps = [
            lambda: self.reserve("r1", "1.21"),
            lambda: self.fill("f1", "buy", 40, count=3, reservation="r1"),
            lambda: self.fill("f2", "sell", 52),
            lambda: self.fill("f3", "sell", 50, count=2),
        ]
        for step in steps:
            step()
            bank = importlib.reload(bank)
        restarted = self.snap()

        other = Path(self._tmp.name) / "uninterrupted.sqlite3"
        bank.init_bank(other, initial_capital_usd="200.00")
        bank.reserve(other, idempotency_key="r1", origin="M5", cycle_id="c-r1", amount_usd="1.21")
        for key, side, price, count, reservation in (
            ("f1", "buy", 40, 3, "r1"),
            ("f2", "sell", 52, 1, None),
            ("f3", "sell", 50, 2, None),
        ):
            bank.record_fill(
                other,
                event_key=key,
                origin="M5",
                position_key=T,
                side=side,
                price_cents=price,
                count=count,
                fee_cents=1,
                reservation_key=reservation,
            )
        uninterrupted = bank.get_snapshot(other)

        for field in ("realized_pnl_usd", "capital_usd", "reserved_usd", "available_usd"):
            self.assertEqual(restarted[field], uninterrupted[field], field)
        self.assertEqual(restarted["positions"], uninterrupted["positions"])


# ------------------------------------------------------------ partial exits and sign


class PartialAndReversalTests(LedgerTestCase):
    def test_partial_close_keeps_the_reservation_until_flat(self):
        self.reserve("r1", "1.21")
        self.fill("f1", "buy", 40, count=3, reservation="r1")

        s = self.fill("f2", "sell", 52)["snapshot"]  # close 1 of 3
        # -1 (entry fee) + 12 (one contract 40→52) - 1 (exit fee) = +10¢
        self.assertEqual(s["realized_pnl_usd"], "0.10")
        self.assertEqual(self.position(s)["net_contracts"], 2)
        self.assertEqual(s["reserved_usd"], "1.21", "a partial exit must not free capital early")

        s = self.fill("f3", "sell", 50, count=2)["snapshot"]  # close the other 2
        # +10 + 2*(50-40) - 1 = +29¢
        self.assertEqual(s["realized_pnl_usd"], "0.29")
        self.assertEqual(self.position(s)["net_contracts"], 0)
        self.assertEqual(s["reserved_usd"], "0.00")

    def test_sign_reversal_closes_fifo_then_opens_the_other_side(self):
        self.reserve("r1", "0.41")
        self.fill("f1", "buy", 40, reservation="r1")
        self.reserve("r2", "0.98")  # the short leg's own worst case: 2 * (100-52) + 2
        s = self.fill("f2", "sell", 52, count=3, fee=2, reservation="r2")["snapshot"]

        pos = self.position(s)
        # -1 + 12 (long lot closed at 52) - 2 = +9¢; then short 2 @ 52 remains open.
        self.assertEqual(s["realized_pnl_usd"], "0.09")
        self.assertEqual(pos["net_contracts"], -2)
        self.assertEqual(pos["open_lots"], [{"side": "short_yes", "price_cents": 52, "count": 2}])
        self.assertEqual(s["reserved_usd"], "1.39", "not flat: both earmarks stay")

        s = self.settle("s1", 0)["snapshot"]
        # short 2 @ 52 resolved NO: +2*52 = +104¢ → +9 + 104 = +113¢
        self.assertEqual(s["realized_pnl_usd"], "1.13")
        self.assertEqual(s["reserved_usd"], "0.00")

    def test_closing_order_is_fifo_not_lifo(self):
        """Two lots at different prices: the policy's order decides the number.

        Found by mutation testing — swapping FIFO for LIFO broke no other test, because
        every other case had a single lot. FIFO closes the 40¢ lot (+10¢); LIFO would
        close the 60¢ lot (-10¢)."""
        self.fill("f1", "buy", 40)
        self.fill("f2", "buy", 60)
        s = self.fill("f3", "sell", 50)["snapshot"]
        # -1 -1 (two entry fees) +10 (40→50, oldest lot) -1 (exit fee) = +7¢
        self.assertEqual(s["realized_pnl_usd"], "0.07")
        self.assertEqual(
            self.position(s)["open_lots"], [{"side": "long_yes", "price_cents": 60, "count": 1}]
        )

    def test_positions_of_different_motors_never_net(self):
        self.fill("m5", "buy", 40, origin="M5")
        s = self.fill("m1", "sell", 52, origin="M1")["snapshot"]
        self.assertEqual(self.position(s, "M5")["net_contracts"], 1)
        self.assertEqual(self.position(s, "M1")["net_contracts"], -1)
        # Only the two fees are realized; nothing was closed across motors.
        self.assertEqual(s["realized_pnl_usd"], "-0.02")


# ----------------------------------------------------- release is not gain; valuation


class ReleaseAndValuationTests(LedgerTestCase):
    def test_release_is_not_a_gain(self):
        self.reserve("r1", "0.41")
        self.fill("f1", "buy", 40, reservation="r1")
        before = self.snap()

        after = bank.release(self.db, idempotency_key="r1")

        self.assertEqual(after["realized_pnl_usd"], before["realized_pnl_usd"])
        self.assertEqual(after["capital_usd"], before["capital_usd"])
        self.assertEqual(after["reserved_usd"], "0.00")
        self.assertNotEqual(after["available_usd"], before["available_usd"])

    def test_open_position_has_unknown_valuation_not_a_guess(self):
        s = self.fill("f1", "buy", 40)["snapshot"]
        pos = self.position(s)
        self.assertEqual(pos["valuation"], "UNKNOWN_NO_MARK")
        # The only realized number is the fee actually paid; no mark, no mid, no 50.
        self.assertEqual(s["realized_pnl_usd"], "-0.01")
        for field in s:
            self.assertNotIn("unrealized", field)
            self.assertNotIn("mtm", field)

    def test_realized_loss_shrinks_what_can_still_be_reserved(self):
        self.fill("f1", "buy", 40)
        self.settle("s1", 0)  # -0.41 realized
        self.assertEqual(self.snap()["available_usd"], "199.59")
        with self.assertRaises(bank.SimulationBankInsufficientFundsError):
            self.reserve("big", "199.60")
        self.reserve("fits", "199.59")


# ------------------------------------------------------------------- invalid evidence


class InvalidEvidenceTests(LedgerTestCase):
    def setUp(self):
        super().setUp()
        self.reserve("r1", "0.41")
        self.fill("f1", "buy", 40, reservation="r1")
        self.before = self.snap()
        self.events_before = self.event_count()

    def assert_nothing_written(self):
        self.assert_unchanged(self.before)
        self.assertEqual(self.event_count(), self.events_before)

    def test_contradicting_identity_is_a_conflict(self):
        with self.assertRaises(bank.SimulationBankConflictError):
            self.fill("f1", "buy", 41, reservation="r1")
        self.assert_nothing_written()

    def test_inconsistent_fee_is_rejected(self):
        for bad in (-1, True, 1.0, "1", None):
            with self.subTest(fee=bad), self.assertRaises(bank.SimulationBankValidationError):
                self.fill("f2", "sell", 52, fee=bad)
        self.assert_nothing_written()

    def test_close_without_origin_is_rejected(self):
        with self.assertRaises(bank.SimulationBankValidationError):
            self.settle("s1", 100, position="KXNADA-26")
        self.assert_nothing_written()

    def test_bad_payout_is_rejected(self):
        for bad in (50, 1, -100, True, 100.0):
            with self.subTest(payout=bad), self.assertRaises(bank.SimulationBankValidationError):
                self.settle("s1", bad)
        self.assert_nothing_written()

    def test_no_fill_or_second_settlement_after_settlement(self):
        self.settle("s1", 100)
        settled = self.snap()
        with self.assertRaises(bank.SimulationBankValidationError):
            self.fill("f9", "buy", 40)
        with self.assertRaises(bank.SimulationBankValidationError):
            self.settle("s2", 0)
        self.assert_unchanged(settled)

    def test_reservation_links_are_checked(self):
        self.reserve("m1r", "0.41", origin="M1")
        self.reserve("r9", "0.41")
        bank.release(self.db, idempotency_key="r9")
        before = self.snap()
        cases = {
            "unknown": "nope",
            "other motor": "m1r",
            "released": "r9",
            "already linked": "r1",
        }
        for label, key in cases.items():
            with self.subTest(label), self.assertRaises(bank.SimulationBankValidationError):
                self.fill(f"x-{label.replace(' ', '-')}", "buy", 40, reservation=key)
        self.assert_unchanged(before)

    def test_corrupted_history_fails_closed(self):
        """A stored row that cannot replay is never skipped: every read raises."""
        con = sqlite3.connect(self.db)
        try:
            con.execute(
                "INSERT INTO simulation_events (event_key, origin, position_key, kind, "
                "payout_cents, created_at) VALUES ('bad', 'M5', 'KXNADA-26', 'SETTLEMENT', 100, 'x')"
            )
            con.commit()
        finally:
            con.close()
        with self.assertRaises(bank.SimulationBankIntegrityError):
            self.snap()


# ------------------------------------------------------ stored history is validated too


class StoredHistoryValidationTests(LedgerTestCase):
    """Replay must enforce the SAME contract as recording (CTO review, 2026-09-22).

    `record_fill` validated its inputs, but `_fold` fed stored rows straight into the
    projection: a stored fee_cents = -1 turned realized P&L from -0.01 into +0.01 with no
    error (reproduced before the fix). A corrupted history must never publish a state —
    not a partial one, and certainly not a flattering one."""

    def setUp(self):
        super().setUp()
        self.fill("f1", "buy", 40)
        self.settle_later = False

    def corrupt(self, sql, params=()):
        con = sqlite3.connect(self.db)
        try:
            # Simulates a file whose rows escaped the table CHECKs (older DDL, manual
            # edit, disk-level damage). The replay guard must hold WITHOUT the CHECKs.
            con.execute("PRAGMA ignore_check_constraints = ON")
            con.execute(sql, params)
            con.commit()
        finally:
            con.close()

    def assert_every_read_and_write_refuses(self):
        with self.assertRaises(bank.SimulationBankIntegrityError) as exc:
            self.snap()
        self.assertIn("seq=", str(exc.exception), "the invalid row must be identified")
        with self.assertRaises(bank.SimulationBankIntegrityError):
            self.reserve("r-after", "0.10")
        with self.assertRaises(bank.SimulationBankIntegrityError):
            self.fill("f-after", "buy", 40)

    def test_corrupted_fill_rows_are_rejected_on_replay(self):
        cases = {
            "negative fee": "fee_cents = -1",
            "fractional fee": "fee_cents = 1.5",
            "text fee": "fee_cents = 'uno'",
            "price 0": "price_cents = 0",
            "price 100": "price_cents = 100",
            "count 0": "count = 0",
            "unknown side": "side = 'hold'",
            "fill with payout": "payout_cents = 100",
        }
        for label, assignment in cases.items():
            with self.subTest(label):
                self.setUp()
                self.corrupt(f"UPDATE simulation_events SET {assignment} WHERE event_key = 'f1'")
                self.assert_every_read_and_write_refuses()

    def test_corrupted_settlement_rows_are_rejected_on_replay(self):
        self.settle("s1", 100)
        cases = {
            "payout 50": "payout_cents = 50",
            "settlement with fee": "fee_cents = 1",
            "settlement with side": "side = 'buy'",
        }
        for label, assignment in cases.items():
            with self.subTest(label):
                self.corrupt(f"UPDATE simulation_events SET {assignment} WHERE event_key = 's1'")
                self.assert_every_read_and_write_refuses()
                # restore for the next subtest
                self.corrupt(
                    "UPDATE simulation_events SET payout_cents = 100, fee_cents = NULL, "
                    "side = NULL WHERE event_key = 's1'"
                )

    def test_table_checks_reject_the_same_rows_at_write_time(self):
        """Defense in depth: without the pragma, SQLite itself refuses the bad row."""
        con = sqlite3.connect(self.db)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE simulation_events SET fee_cents = -1 WHERE event_key = 'f1'")
        finally:
            con.close()
        self.assertEqual(self.snap()["realized_pnl_usd"], "-0.01")


# -------------------------------------------------------- failure, concurrency, migration


class FailureConcurrencyMigrationTests(LedgerTestCase):
    def test_failure_between_writes_leaves_no_partial_state(self):
        """If releasing the earmark fails, the closing event is not recorded either."""
        self.reserve("r1", "0.41")
        self.fill("f1", "buy", 40, reservation="r1")
        before = self.snap()
        events = self.event_count()

        with (
            patch.object(bank, "_release_linked_locked", side_effect=RuntimeError("crash")),
            self.assertRaises(RuntimeError),
        ):
            self.fill("f2", "sell", 52)

        self.assert_unchanged(before)
        self.assertEqual(self.event_count(), events)
        self.assertEqual(self.snap()["reserved_usd"], "0.41")

    def test_concurrent_identical_close_is_counted_once(self):
        self.reserve("r1", "0.41")
        self.fill("f1", "buy", 40, reservation="r1")
        outcomes, errors = [], []
        start = threading.Barrier(8)

        def worker():
            try:
                start.wait()
                outcomes.append(self.fill("f2", "sell", 52)["outcome"])
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), ["RECORDED"] + ["REPLAY"] * 7)
        s = self.snap()
        self.assertEqual(s["realized_pnl_usd"], "0.10")
        self.assertEqual(s["capital_usd"], "200.10")

    def test_existing_bank_file_migrates_without_losing_rows(self):
        """A bank written before the events table existed keeps every reservation."""
        old = Path(self._tmp.name) / "pre-accounting.sqlite3"
        con = sqlite3.connect(old)
        try:
            con.executescript("""
            CREATE TABLE simulation_bank (id INTEGER PRIMARY KEY CHECK (id = 1),
              initial_capital_cents INTEGER NOT NULL, revision INTEGER NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE simulation_reservations (idempotency_key TEXT PRIMARY KEY,
              origin TEXT NOT NULL, cycle_id TEXT NOT NULL, amount_cents INTEGER NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'RELEASED')),
              created_at TEXT NOT NULL, released_at TEXT, evidence_json TEXT);
            INSERT INTO simulation_bank VALUES (1, 20000, 2, 't0', 't1');
            INSERT INTO simulation_reservations VALUES
              ('legacy', 'M5', 'c', 41, 'ACTIVE', 't1', NULL, NULL);
            """)
            con.commit()
        finally:
            con.close()

        s = bank.get_snapshot(old)

        self.assertEqual(s["initial_capital_usd"], "200.00")
        self.assertEqual(s["reserved_usd"], "0.41")
        self.assertEqual([r["idempotency_key"] for r in s["active_reservations"]], ["legacy"])
        self.assertEqual((s["realized_pnl_usd"], s["positions"]), ("0.00", []))
        # And the migrated file accepts accounting events.
        bank.record_fill(
            old,
            event_key="f1",
            origin="M5",
            position_key=T,
            side="buy",
            price_cents=40,
            count=1,
            fee_cents=1,
            reservation_key="legacy",
        )
        self.assertEqual(bank.get_snapshot(old)["realized_pnl_usd"], "-0.01")


if __name__ == "__main__":
    unittest.main()
