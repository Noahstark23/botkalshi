"""C2/7.2 — admission BEFORE quoting, shared budget, fact-dated periods, breaches.

Pinned from the CTO's C2 conditions (2026-09-22):
  - ONE economic admission per proposal: the reservation is taken before quoting; a
    fill that cites the admission consumes it and never reserves again; a new tick
    under the same identity cannot regenerate the admission.
  - shared state updated in ONE transaction across origins (M1, M5, Radar), including
    concentration per correlated thesis.
  - explicit periods by FACT date in America/Los_Angeles (week starts Monday):
    reprocessing historical events must not become today's risk.
  - a shadow fill observed after a quote is NEVER erased: when it has no admission,
    cites a rejected/spent/withdrawn one, or outgrows it, it is recorded with a breach.

Policy under test: SIM_UNIT_1PCT_CAP2_HABITUAL_HALF_AGG3_V1 on a FICTIONAL USD 200.00
bank → unit 2.00, habitual 1.00, per thesis 2.00, open 6.00, daily 6.00; weekly pause
at -12.00 realized, experiment pause at -20.00. Fees in fixtures are synthetic.
A reservation is an earmark, not a loss and not a gain. No network.
"""

from __future__ import annotations

import importlib
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import simulation_bank as bank  # noqa: E402

# Tuesday 2026-09-22 12:00 in Los Angeles (PDT, UTC-7). The accounting week began
# Monday 2026-09-21.
NOW = datetime(2026, 9, 22, 19, 0, tzinfo=UTC)
LAST_WEEK = datetime(2026, 9, 14, 19, 0, tzinfo=UTC)
MONDAY = datetime(2026, 9, 21, 19, 0, tzinfo=UTC)
YESTERDAY = NOW - timedelta(days=1)


class AdmissionTestCase(unittest.TestCase):
    capital = "200.00"

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
        bank.init_bank(self.db, initial_capital_usd=self.capital)

    # -- helpers -------------------------------------------------------------------
    def admit(self, key, risk=100, *, origin="M5", thesis="TH-A", position=None, at=NOW, now=NOW):
        return bank.admit_proposal(
            self.db,
            admission_key=key,
            origin=origin,
            thesis_id=thesis,
            position_key=position or f"MKT-{key}",
            risk_cents=risk,
            proposed_at=at,
            now=now,
        )

    def fill(
        self, key, side="buy", price=50, count=1, fee=1, *, origin="M5", position,
        admission=None, reservation=None, at=NOW, now=NOW,
    ):
        return bank.record_fill(
            self.db,
            event_key=key,
            origin=origin,
            position_key=position,
            side=side,
            price_cents=price,
            count=count,
            fee_cents=fee,
            occurred_at=at,
            admission_key=admission,
            reservation_key=reservation,
            now=now,
        )

    def settle(self, key, payout, *, origin="M5", position, at=NOW, now=NOW):
        return bank.record_settlement(
            self.db,
            event_key=key,
            origin=origin,
            position_key=position,
            payout_cents=payout,
            occurred_at=at,
            now=now,
        )

    def realize_loss(self, key, price, count, *, at):
        """An unadmitted historical buy that settles at 0: a pure fixture loss."""
        self.fill(f"{key}-f", price=price, count=count, fee=0, origin="LEGACY", position=key, at=at)
        self.settle(f"{key}-s", 0, origin="LEGACY", position=key, at=at)

    def snap(self, now=NOW):
        return bank.get_snapshot(self.db, now=now)

    def revision(self):
        return self.snap()["revision"]

    def active_reservations(self):
        return {r["idempotency_key"]: r["amount_usd"] for r in self.snap()["active_reservations"]}


class PolicyDecisionTests(AdmissionTestCase):
    def test_admitted_proposal_reserves_its_risk_in_the_same_call(self):
        result = self.admit("p1", 100)
        self.assertEqual(result["outcome"], "RECORDED")
        self.assertEqual(result["decision"], "ADMITTED")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["reservation_key"], "adm:p1")
        self.assertEqual(self.active_reservations(), {"adm:p1": "1.00"})
        snap = result["snapshot"]
        self.assertEqual(snap["reserved_usd"], "1.00")
        self.assertEqual(snap["today_new_risk_usd"], "1.00")
        self.assertEqual(snap["open_risk_usd"], "1.00")
        # An earmark is not a loss: capital and realized P&L do not move.
        self.assertEqual(snap["capital_usd"], "200.00")
        self.assertEqual(snap["realized_pnl_usd"], "0.00")
        self.assertIs(snap["execution_authorized"], False)
        self.assertEqual(result["limits_usd"]["habitual"], "1.00")
        self.assertEqual(result["limits_usd"]["unit"], "2.00")

    def test_proposal_above_habitual_is_rejected_never_bumped(self):
        result = self.admit("p1", 101)
        self.assertEqual(result["decision"], "REJECTED")
        self.assertEqual(result["reasons"], ["SIZE_ABOVE_HABITUAL"])
        self.assertIsNone(result["reservation_key"])
        self.assertEqual(self.active_reservations(), {})
        self.assertEqual(self.snap()["today_new_risk_usd"], "0.00")

    def test_rejection_is_recorded_once_and_consumes_nothing(self):
        before = self.revision()
        self.admit("p1", 101)
        self.assertEqual(self.revision(), before + 1)
        self.assertEqual(self.snap()["reserved_usd"], "0.00")

    def test_thesis_cap_is_shared_across_origins(self):
        """M1 and M5 on the SAME correlated thesis share one unit; a third origin is refused."""
        self.assertEqual(self.admit("m1", 100, origin="M1", thesis="TH-X")["decision"], "ADMITTED")
        self.assertEqual(self.admit("m5", 100, origin="M5", thesis="TH-X")["decision"], "ADMITTED")
        radar = self.admit("r1", 1, origin="RADAR", thesis="TH-X")
        self.assertEqual(radar["decision"], "REJECTED")
        self.assertEqual(radar["reasons"], ["THESIS_CAP"])
        # A different thesis is not concentrated with TH-X.
        self.assertEqual(self.admit("r2", 1, origin="RADAR", thesis="TH-Y")["decision"], "ADMITTED")

    def test_open_and_daily_caps_are_three_units(self):
        for i in range(6):
            self.assertEqual(self.admit(f"p{i}", 100, thesis=f"T{i}")["decision"], "ADMITTED")
        seventh = self.admit("p6", 1, thesis="T6")
        self.assertEqual(seventh["decision"], "REJECTED")
        self.assertEqual(seventh["reasons"], ["OPEN_CAP", "DAILY_CAP"])

    def test_withdrawal_frees_open_risk_but_not_the_daily_budget(self):
        for i in range(6):
            self.admit(f"p{i}", 100, thesis=f"T{i}")
        withdrawn = bank.withdraw_admission(self.db, admission_key="p0", now=NOW)
        self.assertIs(withdrawn["released"], True)
        self.assertEqual(withdrawn["snapshot"]["open_risk_usd"], "5.00")
        self.assertEqual(withdrawn["snapshot"]["today_new_risk_usd"], "6.00")
        again = self.admit("p6", 1, thesis="T6")
        self.assertEqual(again["reasons"], ["DAILY_CAP"])

    def test_daily_budget_renews_at_los_angeles_midnight_not_utc(self):
        for i in range(3):
            self.admit(f"p{i}", 100, thesis=f"T{i}")
        for i in range(3):
            bank.withdraw_admission(self.db, admission_key=f"p{i}", now=NOW)
        # Fill the rest of 22-sep's budget.
        for i in range(3, 6):
            self.admit(f"p{i}", 100, thesis=f"T{i}")
        for i in range(3, 6):
            bank.withdraw_admission(self.db, admission_key=f"p{i}", now=NOW)
        # 06:59 UTC on 23-sep is still 23:59 on 22-sep in Los Angeles: budget spent.
        late = datetime(2026, 9, 23, 6, 59, tzinfo=UTC)
        self.assertEqual(self.admit("late", 1, thesis="T9", at=late, now=late)["reasons"], ["DAILY_CAP"])
        # 07:01 UTC on 23-sep is 00:01 on 23-sep in Los Angeles: a new day.
        dawn = datetime(2026, 9, 23, 7, 1, tzinfo=UTC)
        fresh = self.admit("dawn", 1, thesis="T9", at=dawn, now=dawn)
        self.assertEqual(fresh["decision"], "ADMITTED")
        self.assertEqual(fresh["snapshot"]["risk_day"], "2026-09-23")
        self.assertEqual(fresh["snapshot"]["today_new_risk_usd"], "0.01")

    def test_close_does_not_give_back_the_daily_budget(self):
        self.admit("p1", 100, position="MKT")
        self.fill("f1", "buy", 50, admission="p1", position="MKT")
        closed = self.fill("f2", "sell", 60, position="MKT")
        self.assertEqual(closed["snapshot"]["active_reservations"], [])
        self.assertEqual(closed["snapshot"]["open_risk_usd"], "0.00")
        self.assertEqual(closed["snapshot"]["today_new_risk_usd"], "1.00")
        self.assertEqual(closed["snapshot"]["realized_pnl_usd"], "0.08")

    def test_weekly_pause_uses_the_accounting_week_of_the_fact(self):
        self.realize_loss("LOSS", 99, 13, at=MONDAY)  # -12.87 realized on Monday
        paused = self.admit("p1", 50)
        self.assertEqual(paused["decision"], "REJECTED")
        self.assertIn("PAUSED_WEEKLY", paused["reasons"])
        self.assertNotIn("PAUSED_EXPERIMENT", paused["reasons"])
        # The same loss dated LAST week does not pause this week.
        next_monday = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)
        later = self.admit("p2", 50, at=next_monday, now=next_monday)
        self.assertEqual(later["decision"], "ADMITTED", later["reasons"])
        self.assertEqual(later["snapshot"]["week_start"], "2026-09-28")
        self.assertEqual(later["snapshot"]["week_realized_pnl_usd"], "0.00")

    def test_experiment_pause_is_cumulative_across_weeks(self):
        self.realize_loss("LOSS", 99, 21, at=LAST_WEEK)  # -20.79, not this week
        paused = self.admit("p1", 50)
        self.assertEqual(paused["decision"], "REJECTED")
        self.assertIn("PAUSED_EXPERIMENT", paused["reasons"])
        self.assertNotIn("PAUSED_WEEKLY", paused["reasons"])

    def test_experiment_headroom_counts_open_risk(self):
        self.realize_loss("LOSS", 78, 25, at=LAST_WEEK)  # -19.50: 0.50 left before -20
        snap = self.snap()
        self.assertEqual(snap["capital_usd"], "180.50")
        self.assertEqual(snap["risk_policy"]["habitual"], "0.90")  # unit shrank to 1.80
        self.assertEqual(self.admit("p1", 60)["reasons"], ["EXPERIMENT_CAP"])
        self.assertEqual(self.admit("p2", 50)["decision"], "ADMITTED")
        self.assertEqual(self.admit("p3", 1, thesis="TH-B")["reasons"], ["EXPERIMENT_CAP"])

    def test_realized_loss_shrinks_the_unit_derived_from_capital(self):
        self.realize_loss("LOSS", 50, 10, at=LAST_WEEK)  # -5.00 → capital 195.00
        snap = self.snap()
        self.assertEqual(snap["risk_policy"]["unit"], "1.95")
        self.assertEqual(snap["risk_policy"]["habitual"], "0.97")
        self.assertEqual(snap["risk_policy"]["max_open"], "5.85")
        self.assertEqual(self.admit("p1", 98)["reasons"], ["SIZE_ABOVE_HABITUAL"])
        self.assertEqual(self.admit("p2", 97)["decision"], "ADMITTED")


class TinyCapitalTests(AdmissionTestCase):
    capital = "0.50"

    def test_zero_habitual_admits_nothing(self):
        result = self.admit("p1", 1)
        self.assertEqual(result["decision"], "REJECTED")
        self.assertIn("SIZE_ABOVE_HABITUAL", result["reasons"])


class IdentityAndReplayTests(AdmissionTestCase):
    def test_identical_replay_returns_the_stored_decision_without_charging(self):
        first = self.admit("p1", 100)
        before = self.revision()
        replay = self.admit("p1", 100)
        self.assertEqual(replay["outcome"], "REPLAY")
        self.assertEqual(replay["decision"], "ADMITTED")
        self.assertEqual(replay["reservation_key"], first["reservation_key"])
        self.assertEqual(self.revision(), before)
        self.assertEqual(self.snap()["today_new_risk_usd"], "1.00")

    def test_new_tick_cannot_regenerate_the_identity(self):
        self.admit("p1", 100)
        with self.assertRaises(bank.SimulationBankConflictError):
            self.admit("p1", 100, at=NOW + timedelta(seconds=1))
        with self.assertRaises(bank.SimulationBankConflictError):
            self.admit("p1", 99)
        with self.assertRaises(bank.SimulationBankConflictError):
            self.admit("p1", 100, thesis="TH-OTHER")
        self.assertEqual(self.snap()["today_new_risk_usd"], "1.00")

    def test_rejected_replay_stays_rejected_after_capacity_frees(self):
        for i in range(6):
            self.admit(f"p{i}", 100, thesis=f"T{i}")
        self.assertEqual(self.admit("late", 1, thesis="T9")["decision"], "REJECTED")
        tomorrow = NOW + timedelta(days=1)
        replay = self.admit("late", 1, thesis="T9", now=tomorrow)
        self.assertEqual(replay["outcome"], "REPLAY")
        self.assertEqual(replay["decision"], "REJECTED")
        self.assertEqual(replay["reasons"], ["OPEN_CAP", "DAILY_CAP"])

    def test_old_decision_replays_but_old_new_proposal_is_refused(self):
        self.admit("p1", 100)
        much_later = NOW + timedelta(days=3)
        self.assertEqual(self.admit("p1", 100, now=much_later)["outcome"], "REPLAY")
        before = self.revision()
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "PROPOSAL_NOT_CURRENT"):
            self.admit("hist", 50, at=NOW - timedelta(minutes=6))
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "PROPOSAL_IN_FUTURE"):
            self.admit("fut", 50, at=NOW + timedelta(minutes=2))
        self.assertEqual(self.revision(), before)

    def test_naive_proposal_time_is_refused(self):
        with self.assertRaises(bank.SimulationBankValidationError):
            self.admit("p1", 50, at=datetime(2026, 9, 22, 12, 0))

    def test_decision_survives_a_restart(self):
        self.admit("p1", 100)
        importlib.reload(bank)
        replay = self.admit("p1", 100)
        self.assertEqual((replay["outcome"], replay["decision"]), ("REPLAY", "ADMITTED"))
        self.assertEqual(self.snap()["reserved_usd"], "1.00")

    def test_withdraw_is_idempotent_and_rejected_has_nothing_to_release(self):
        self.admit("p1", 100)
        self.assertIs(bank.withdraw_admission(self.db, admission_key="p1", now=NOW)["released"], True)
        self.assertIs(bank.withdraw_admission(self.db, admission_key="p1", now=NOW)["released"], False)
        self.admit("big", 500)
        self.assertIs(bank.withdraw_admission(self.db, admission_key="big", now=NOW)["released"], False)
        with self.assertRaises(bank.SimulationBankNotFoundError):
            bank.withdraw_admission(self.db, admission_key="nope", now=NOW)


class FillByAdmissionTests(AdmissionTestCase):
    def test_fill_consumes_the_admission_reservation_once(self):
        self.admit("p1", 100, position="MKT")
        result = self.fill("f1", "buy", 50, admission="p1", position="MKT")
        self.assertIsNone(result["breach"])
        snap = result["snapshot"]
        # Still ONE reservation of 1.00: the fill did not reserve again.
        self.assertEqual(self.active_reservations(), {"adm:p1": "1.00"})
        self.assertEqual(snap["reserved_usd"], "1.00")
        self.assertEqual(snap["today_new_risk_usd"], "1.00")
        self.assertEqual(snap["open_risk_usd"], "1.00")
        self.assertEqual(snap["unreserved_open_exposure_usd"], "0.00")
        self.assertEqual(snap["breaches"], [])

    def test_fill_replay_is_a_noop_and_cites_the_caller_fields(self):
        self.admit("p1", 100, position="MKT")
        self.fill("f1", "buy", 50, admission="p1", position="MKT")
        before = self.revision()
        replay = self.fill("f1", "buy", 50, admission="p1", position="MKT")
        self.assertEqual(replay["outcome"], "REPLAY")
        self.assertEqual(self.revision(), before)
        with self.assertRaises(bank.SimulationBankConflictError):
            self.fill("f1", "buy", 51, admission="p1", position="MKT")

    def test_partial_fills_of_one_quote_draw_from_the_same_admission(self):
        """A resting quote can fill in pieces: each piece consumes the SAME admitted risk,
        cumulatively, and is a breach only once the total outgrows it."""
        self.admit("p1", 100, position="MKT")
        first = self.fill("f1", "buy", 50, admission="p1", position="MKT")  # 0.51
        second = self.fill("f2", "buy", 40, admission="p1", position="MKT")  # 0.41 → 0.92
        self.assertIsNone(first["breach"])
        self.assertIsNone(second["breach"])
        self.assertEqual(self.active_reservations(), {"adm:p1": "1.00"})
        self.assertEqual(second["snapshot"]["today_new_risk_usd"], "1.00")
        third = self.fill("f3", "buy", 50, admission="p1", position="MKT")  # 0.51 → 1.43
        self.assertEqual(third["outcome"], "RECORDED")
        self.assertEqual(third["breach"], "ADMISSION_EXCEEDED")
        snap = third["snapshot"]
        self.assertEqual(snap["positions"][0]["net_contracts"], 3)
        # Worst case 1.40 against a 1.00 reservation: 0.40 open and uncovered.
        self.assertEqual(snap["reserved_usd"], "1.00")
        self.assertEqual(snap["unreserved_open_exposure_usd"], "0.40")
        self.assertEqual(snap["open_risk_usd"], "1.40")
        # Daily: 1.00 admitted + 0.43 that no admission covered (1.43 - 1.00).
        self.assertEqual(snap["today_new_risk_usd"], "1.43")
        self.assertEqual(
            snap["breaches"],
            [{"event_key": "f3", "origin": "M5", "position_key": "MKT", "breach": "ADMISSION_EXCEEDED"}],
        )

    def test_fill_after_the_position_closed_cannot_reuse_the_admission(self):
        self.admit("p1", 100, position="MKT")
        self.fill("f1", "buy", 50, admission="p1", position="MKT")
        self.fill("f2", "sell", 55, position="MKT")  # flat → reservation released
        reopened = self.fill("f3", "buy", 30, admission="p1", position="MKT")
        self.assertEqual(reopened["breach"], "ADMISSION_EXCEEDED")
        self.assertEqual(reopened["snapshot"]["unreserved_open_exposure_usd"], "0.30")

    def test_withdrawal_after_a_partial_fill_keeps_the_reservation(self):
        self.admit("p1", 100, position="MKT")
        self.fill("f1", "buy", 50, admission="p1", position="MKT")
        self.assertIs(bank.withdraw_admission(self.db, admission_key="p1", now=NOW)["released"], False)
        self.assertEqual(self.active_reservations(), {"adm:p1": "1.00"})

    def test_fill_after_the_quote_was_withdrawn_is_not_erased(self):
        """The CTO's case: a shadow fill observed after emitting a quote whose admission
        was withdrawn must stay in the history, flagged."""
        self.admit("p1", 100, position="MKT")
        bank.withdraw_admission(self.db, admission_key="p1", now=NOW)
        late = self.fill("f1", "buy", 40, admission="p1", position="MKT")
        self.assertEqual(late["outcome"], "RECORDED")
        self.assertEqual(late["breach"], "ADMISSION_EXCEEDED")
        snap = late["snapshot"]
        self.assertEqual(snap["positions"][0]["net_contracts"], 1)
        self.assertEqual(snap["reserved_usd"], "0.00")
        self.assertEqual(snap["unreserved_open_exposure_usd"], "0.40")
        self.assertEqual(snap["open_risk_usd"], "0.40")
        # Daily: 1.00 admitted + 0.41 uncovered opening (price + fee) — never less.
        self.assertEqual(snap["today_new_risk_usd"], "1.41")

    def test_fill_larger_than_admitted_is_flagged_and_excess_counted(self):
        self.admit("p1", 60, position="MKT")
        big = self.fill("f1", "buy", 50, count=2, fee=1, admission="p1", position="MKT")
        self.assertEqual(big["breach"], "ADMISSION_EXCEEDED")
        snap = big["snapshot"]
        self.assertEqual(snap["reserved_usd"], "0.60")
        self.assertEqual(snap["unreserved_open_exposure_usd"], "0.40")
        self.assertEqual(snap["open_risk_usd"], "1.00")
        self.assertEqual(snap["today_new_risk_usd"], "1.01")  # 0.60 + (1.01 - 0.60)

    def test_unadmitted_fill_is_recorded_and_counted_where_it_happened(self):
        result = self.fill("f1", "buy", 99, count=3, fee=0, position="MKT")
        self.assertEqual(result["breach"], "UNADMITTED")
        snap = result["snapshot"]
        self.assertEqual(snap["today_new_risk_usd"], "2.97")
        self.assertEqual(snap["open_risk_usd"], "2.97")
        self.assertEqual(snap["unreserved_open_exposure_usd"], "2.97")
        # The exposure is real to the next proposal: 2.97 + 3 × 1.00 = 5.97 fits, and
        # 0.04 more (6.01) does not.
        for i in range(3):
            self.assertEqual(self.admit(f"p{i}", 100, thesis=f"T{i}")["decision"], "ADMITTED")
        self.assertEqual(self.admit("p3", 4, thesis="T3")["reasons"], ["OPEN_CAP", "DAILY_CAP"])
        self.assertEqual(self.admit("p4", 3, thesis="T4")["decision"], "ADMITTED")

    def test_fill_citing_a_rejected_admission_is_unadmitted(self):
        self.admit("big", 500, position="MKT")
        result = self.fill("f1", "buy", 50, admission="big", position="MKT")
        self.assertEqual(result["breach"], "UNADMITTED")

    def test_pure_close_without_admission_is_not_a_breach(self):
        self.admit("p1", 100, position="MKT")
        self.fill("f1", "buy", 50, admission="p1", position="MKT")
        close = self.fill("f2", "sell", 45, position="MKT")
        self.assertIsNone(close["breach"])
        self.assertEqual(close["snapshot"]["breaches"], [])

    def test_contradictory_identity_is_refused_before_writing(self):
        self.admit("p1", 100, origin="M5", position="MKT")
        before = self.revision()
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "ADMISSION_IDENTITY_MISMATCH"):
            self.fill("f1", admission="p1", origin="M1", position="MKT")
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "ADMISSION_IDENTITY_MISMATCH"):
            self.fill("f1", admission="p1", origin="M5", position="OTHER")
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "UNKNOWN_ADMISSION"):
            self.fill("f1", admission="nope", position="MKT")
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "not both"):
            self.fill("f1", admission="p1", reservation="adm:p1", position="MKT")
        self.assertEqual(self.revision(), before)
        self.assertEqual(self.snap()["positions"], [])

    def test_future_fill_is_refused(self):
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "future"):
            self.fill("f1", position="MKT", at=NOW + timedelta(minutes=2))


class FactDatedPeriodTests(AdmissionTestCase):
    def test_historical_fill_imported_today_is_not_todays_risk(self):
        result = self.fill("f1", "buy", 99, count=3, fee=0, position="MKT", at=YESTERDAY)
        self.assertEqual(result["breach"], "UNADMITTED")
        snap = result["snapshot"]
        self.assertEqual(snap["today_new_risk_usd"], "0.00")
        # ...but its open exposure is still open risk today.
        self.assertEqual(snap["open_risk_usd"], "2.97")

    def test_week_realized_follows_the_settlement_fact_date(self):
        self.fill("f1", "buy", 40, fee=0, position="MKT", at=LAST_WEEK)
        self.settle("s1", 100, position="MKT", at=MONDAY)
        snap = self.snap()
        self.assertEqual(snap["week_start"], "2026-09-21")
        self.assertEqual(snap["week_realized_pnl_usd"], "0.60")
        self.assertEqual(snap["accounting_zone"], "America/Los_Angeles")

    def test_sunday_evening_los_angeles_belongs_to_the_previous_week(self):
        # 2026-09-28 05:00 UTC is Sunday 27-sep 22:00 in Los Angeles.
        sunday_night = datetime(2026, 9, 28, 5, 0, tzinfo=UTC)
        self.fill("f1", "buy", 40, fee=0, position="MKT", at=sunday_night, now=sunday_night)
        self.settle("s1", 0, position="MKT", at=sunday_night, now=sunday_night)
        self.assertEqual(self.snap(now=sunday_night)["week_realized_pnl_usd"], "-0.40")
        monday = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)
        self.assertEqual(self.snap(now=monday)["week_start"], "2026-09-28")
        self.assertEqual(self.snap(now=monday)["week_realized_pnl_usd"], "0.00")


class ConcurrencyTests(AdmissionTestCase):
    def test_concurrent_origins_never_commit_more_than_the_open_cap(self):
        results, errors = [], []
        barrier = threading.Barrier(12)

        def worker(i):
            try:
                barrier.wait()
                results.append(
                    self.admit(f"p{i}", 100, origin=("M1", "M5", "RADAR")[i % 3], thesis=f"T{i}")
                )
            except Exception as exc:  # noqa: BLE001 — surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        decisions = [r["decision"] for r in results]
        self.assertEqual(decisions.count("ADMITTED"), 6)
        self.assertEqual(decisions.count("REJECTED"), 6)
        snap = self.snap()
        self.assertEqual(snap["reserved_usd"], "6.00")
        self.assertEqual(snap["today_new_risk_usd"], "6.00")

    def test_concurrent_identical_admissions_admit_once(self):
        results, errors = [], []
        barrier = threading.Barrier(8)

        def worker():
            try:
                barrier.wait()
                results.append(self.admit("same", 100))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(r["outcome"] for r in results), ["RECORDED"] + ["REPLAY"] * 7)
        self.assertEqual(self.snap()["reserved_usd"], "1.00")


class StoredAdmissionIntegrityTests(AdmissionTestCase):
    def test_admission_table_refuses_an_admitted_row_without_reservation(self):
        con = sqlite3.connect(self.db)
        self.addCleanup(con.close)
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO simulation_admissions (admission_key, origin, thesis_id, "
                "position_key, risk_cents, proposed_at, risk_day, decision, reasons_json, "
                "reservation_key, policy_version, evidence_json, created_at) "
                "VALUES ('x','M5','T','P',100,?, '2026-09-22','ADMITTED','[]',NULL,'v',NULL,?)",
                (NOW.isoformat(), NOW.isoformat()),
            )

    def test_stored_breach_outside_the_vocabulary_does_not_replay(self):
        self.fill("f1", "buy", 50, position="MKT")
        con = sqlite3.connect(self.db)
        self.addCleanup(con.close)
        con.execute("PRAGMA ignore_check_constraints = ON")
        con.execute("UPDATE simulation_events SET breach = 'FORGIVEN' WHERE event_key = 'f1'")
        con.commit()
        with self.assertRaisesRegex(bank.SimulationBankIntegrityError, "seq=1"):
            self.snap()


if __name__ == "__main__":
    unittest.main()
