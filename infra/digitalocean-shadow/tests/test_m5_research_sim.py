"""M5 research REST — acceptance of the integrated path (CTO, 2026-09-22).

Captured book → M1 strict review → candidate → admission + reservation → activation →
fill in a LATER observation, linked to the same admission → accounting event.

Acceptance list, one class each:
  - rejected admission ⇒ no active quote;
  - admitted ⇒ persisted quote, and a later fill linked to the SAME admission;
  - replay / restart ⇒ no double reservation, fill or result;
  - withdrawn / released reservation ⇒ never reactivated by a replay;
  - missing or stale fair, invalid book or incompatible precision ⇒ blocked;
  - withdrawal / close ⇒ the daily budget is not given back;
  - identical accounting before and after a restart.

Fixtures are synthetic: tickers shaped like KXMLBGAME, fair 0.48 from a named fixture
source, fee multiplier 1/2 declared as fixture evidence — none of it is market data.
The FICTIONAL bank starts at USD 200.00 (habitual 1.00). No network.
"""

from __future__ import annotations

import ast
import importlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import m5_research_sim as sim  # noqa: E402
import research_runner  # noqa: E402
import simulation_bank as bank  # noqa: E402
from m1_observation import review_m1_book  # noqa: E402

EVENT = "KXMLBGAME-26SEP221905NYYBOS"
TICKER = f"{EVENT}-NYY"
T0 = datetime(2026, 9, 22, 19, 0, tzinfo=UTC)
KICKOFF = T0 + timedelta(hours=3)
CALM = ([["0.4500", "10"]], [["0.5000", "10"]])  # YES bid 45, YES ask 50
BUY_CROSS = ([["0.4300", "10"]], [["0.5600", "10"]])  # YES ask 44 < our bid 45
SELL_CROSS = ([["0.5200", "10"]], [["0.4700", "10"]])  # YES bid 52 > our ask 51


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class SimTestCase(unittest.TestCase):
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

    # -- fixtures ------------------------------------------------------------------
    @staticmethod
    def market(book=CALM, *, now, ticker=TICKER, event=EVENT, status="open"):
        yes, no = book
        return {
            "ticker": ticker,
            "event_ticker": event,
            "status": status,
            "close_time": (now + timedelta(hours=4)).isoformat(),
            "book_observed_at": (now - timedelta(seconds=2)).isoformat(),
            "levels": {},
            "m1_book": {"yes_bids": yes, "no_bids": no},
        }

    @staticmethod
    def inputs(
        now,
        *,
        fair="0.4800",
        fair_age=30,
        fee_age=60,
        kickoff=KICKOFF,
        ticker=TICKER,
        event=EVENT,
        fee=True,
    ):
        data = {
            "schema_version": sim.SCHEMA_INPUT,
            "fairs": [
                {
                    "ticker": ticker,
                    "event_ticker": event,
                    "fair_prob": fair,
                    "source": "fixture-test",
                    "observed_at": (now - timedelta(seconds=fair_age)).isoformat(),
                    "commence_time": kickoff.isoformat(),
                }
            ],
            "fees": [],
        }
        if fee:
            data["fees"].append(
                {
                    "event_ticker": event,
                    "series_ticker": "KXMLBGAME",
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": "1/2",
                    "source": "series",
                    "observed_at": (now - timedelta(seconds=fee_age)).isoformat(),
                }
            )
        return data

    def cycle(self, pid, now, markets=None, inputs="default", problem=None, evaluated_at=None):
        """Build the capture at `now`; evaluate it at `evaluated_at` (default: now) — a
        replayed cycle is the SAME packet read again later."""
        markets = [self.market(now=now)] if markets is None else markets
        packet = {
            "packet_id": pid,
            "generated_at": (now - timedelta(seconds=5)).isoformat(),
            "kalshi": {"markets": markets},
        }
        m1 = {
            "cycle_id": pid,
            "reviews": [
                review_m1_book(research_runner._m1_book_input(pid, m), now=now) for m in markets
            ],
        }
        return sim.run_cycle(
            bank_db=self.db,
            packet=packet,
            m1_review=m1,
            inputs=self.inputs(now) if inputs == "default" else inputs,
            inputs_problem=problem,
            now=evaluated_at or now,
        )

    def snap(self, now):
        return bank.get_snapshot(self.db, now=now)

    def m5_events(self):
        import sqlite3

        con = sqlite3.connect(self.db)
        self.addCleanup(con.close)
        return con.execute(
            "SELECT side, price_cents, count, fee_cents, admission_key, reservation_key, breach "
            "FROM simulation_events ORDER BY seq"
        ).fetchall()

    def admissions(self):
        return bank.list_admissions(self.db, origin="M5")


class AdmittedQuoteTests(SimTestCase):
    def test_admitted_quote_is_persisted_and_active(self):
        report = self.cycle("c1", at(0))
        self.assertEqual(report["status"], "OK")
        [prop] = report["proposals"]
        self.assertEqual((prop["decision"], prop["active"]), ("ADMITTED", True))
        self.assertEqual((prop["bid_cents"], prop["ask_cents"], prop["size"]), (45, 51, 1))
        [q] = self.admissions()
        self.assertEqual(q["activation_observation"], "c1")
        self.assertEqual(q["evidence"]["cohort"], "m5-research-rest-v1")
        self.assertEqual(q["evidence"]["fair"]["source"], "fixture-test")
        self.assertEqual(q["evidence"]["fee"]["fee_multiplier"], "1/2")
        self.assertEqual(q["thesis_id"], EVENT)
        self.assertEqual(q["position_key"], f"m5r1:{TICKER}")

    def test_bilateral_quote_reserves_one_bound_not_two_budgets(self):
        self.cycle("c1", at(0))
        snap = self.snap(at(0))
        # max(45 + fee1(45), (100 - 51) + fee1(51)) = max(46, 50) = 0.50 — never 0.96.
        self.assertEqual(snap["reserved_usd"], "0.50")
        self.assertEqual(snap["today_new_risk_usd"], "0.50")

    def test_later_fill_is_linked_to_the_same_admission(self):
        self.cycle("c1", at(0))
        report = self.cycle("c2", at(1), [self.market(BUY_CROSS, now=at(1))])
        [evaluation] = report["evaluations"]
        self.assertEqual(evaluation["status"], "EVALUATED")
        self.assertEqual(
            evaluation["fills"], [{"side": "buy", "price_cents": 45, "count": 1, "fee_cents": 1}]
        )
        [q] = self.admissions()
        [event] = self.m5_events()
        self.assertEqual(event, ("buy", 45, 1, 1, q["admission_key"], q["reservation_key"], None))
        snap = self.snap(at(1))
        self.assertEqual(snap["reserved_usd"], "0.50")  # consumed, never reserved again
        self.assertEqual(snap["today_new_risk_usd"], "0.50")
        self.assertEqual(snap["realized_pnl_usd"], "-0.01")  # the maker fee, once
        self.assertEqual(report["bank"]["m5_positions"][0]["net_contracts"], 1)

    def test_generating_observation_never_fills_its_quote(self):
        self.cycle("c1", at(0))
        # The same observation again (restart mid-cycle) is skipped, not evaluated.
        again = self.cycle("c1", at(0))
        self.assertEqual(again["evaluations"][0]["status"], "SKIPPED_GENERATING_OBSERVATION")
        [q] = self.admissions()
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "SAME_OBSERVATION"):
            bank.record_quote_observation(
                self.db,
                admission_key=q["admission_key"],
                observation_id="c1",
                observed_at=at(1),
                outcome="EVALUATED",
                fills=[],
                now=at(1),
            )
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "NOT_AFTER"):
            bank.record_quote_observation(
                self.db,
                admission_key=q["admission_key"],
                observation_id="older",
                observed_at=at(-1),
                outcome="EVALUATED",
                fills=[],
                now=at(1),
            )
        self.assertEqual(self.m5_events(), [])

    def test_equal_touch_is_not_a_fill(self):
        self.cycle("c1", at(0))
        touch = ([["0.4400", "10"]], [["0.5500", "10"]])  # YES ask 45 == our bid 45
        report = self.cycle("c2", at(1), [self.market(touch, now=at(1))])
        self.assertEqual(report["evaluations"][0]["fills"], [])

    def test_both_sides_filled_withdraws_and_releases_without_restoring_daily(self):
        self.cycle("c1", at(0))
        self.cycle("c2", at(1), [self.market(BUY_CROSS, now=at(1))])
        report = self.cycle("c3", at(2), [self.market(SELL_CROSS, now=at(2))])
        self.assertEqual(report["evaluations"][0]["fills"][0]["side"], "sell")
        [w] = report["withdrawals"]
        self.assertEqual((w["reason"], w["reservation_released"]), ("FULLY_FILLED", True))
        snap = self.snap(at(2))
        self.assertEqual(snap["reserved_usd"], "0.00")
        self.assertEqual(snap["today_new_risk_usd"], "0.50")  # not given back
        self.assertEqual(snap["realized_pnl_usd"], "0.04")  # 51 - 45 - 1 - 1
        self.assertEqual(snap["breaches"], [])


class RejectedAdmissionTests(SimTestCase):
    def test_rejected_admission_yields_no_active_quote(self):
        # Another origin already holds the whole per-thesis unit on this event.
        for i in range(2):
            bank.admit_proposal(
                self.db,
                admission_key=f"m1-{i}",
                origin="M1",
                thesis_id=EVENT,
                position_key=f"M1-{i}",
                risk_cents=100,
                proposed_at=at(0),
                now=at(0),
            )
        report = self.cycle("c1", at(0))
        [prop] = report["proposals"]
        self.assertEqual(prop["decision"], "REJECTED")
        self.assertIn("THESIS_CAP", prop["reasons"])
        self.assertIs(prop["active"], False)
        self.assertEqual(sim._live_quotes(self.db), [])
        # A later crossing book produces nothing: there is no active quote to fill.
        later = self.cycle("c2", at(1), [self.market(BUY_CROSS, now=at(1))])
        self.assertEqual(later["evaluations"], [])
        self.assertEqual(self.m5_events(), [])


class ReplayAndRestartTests(SimTestCase):
    def test_replayed_cycle_does_not_double_reservation(self):
        self.cycle("c1", at(0))
        again = self.cycle("c1", at(0))
        self.assertEqual(again["proposals"], [])  # the ticker already has a live quote
        self.assertEqual(len(self.admissions()), 1)
        self.assertEqual(self.snap(at(0))["reserved_usd"], "0.50")

    def test_restart_replays_fill_and_accounting_identically(self):
        self.cycle("c1", at(0))
        self.cycle("c2", at(1), [self.market(BUY_CROSS, now=at(1))])
        before = self.snap(at(1))
        importlib.reload(bank)
        importlib.reload(sim)
        # The same observation evaluated again after the restart: stored, not recomputed.
        [q] = self.admissions()
        replay = bank.record_quote_observation(
            self.db,
            admission_key=q["admission_key"],
            observation_id="c2",
            observed_at=datetime.fromisoformat(q["last_observed_at"]),
            outcome="EVALUATED",
            fills=[{"side": "buy", "price_cents": 45, "count": 1, "fee_cents": 1}],
            detail={"rules": ["ask 44 < bid 45"]},
            now=at(1),
        )
        self.assertEqual(replay["outcome"], "REPLAY")
        after = self.snap(at(1))
        self.assertEqual(before, after)
        self.assertEqual(len(self.m5_events()), 1)

    def test_changed_result_for_a_recorded_observation_is_a_conflict(self):
        self.cycle("c1", at(0))
        self.cycle("c2", at(1), [self.market(BUY_CROSS, now=at(1))])
        [q] = self.admissions()
        with self.assertRaises(bank.SimulationBankConflictError):
            bank.record_quote_observation(
                self.db,
                admission_key=q["admission_key"],
                observation_id="c2",
                observed_at=datetime.fromisoformat(q["last_observed_at"]),
                outcome="EVALUATED",
                fills=[],
                now=at(1),
            )

    def test_crash_between_admission_and_activation_is_swept_next_cycle(self):
        with (
            patch.object(bank, "activate_admission", side_effect=RuntimeError("crash")),
            self.assertRaises(RuntimeError),
        ):
            self.cycle("c1", at(0))
        [q] = self.admissions()
        self.assertIsNone(q["activated_at"])
        self.assertIs(q["reservation_active"], True)
        report = self.cycle("c2", at(1))
        swept = [w for w in report["withdrawals"] if w["reason"] == "NEVER_ACTIVATED"]
        self.assertEqual(len(swept), 1)
        self.assertIs(swept[0]["reservation_released"], True)
        # c2 then proposes afresh: one live quote, one reservation.
        self.assertEqual(self.snap(at(1))["reserved_usd"], "0.50")
        self.assertEqual(self.snap(at(1))["today_new_risk_usd"], "1.00")  # both admitted


class NoReactivationTests(SimTestCase):
    def test_replay_of_a_withdrawn_admission_does_not_reactivate(self):
        self.cycle("c1", at(0))
        [q] = self.admissions()
        bank.withdraw_admission(
            self.db, admission_key=q["admission_key"], reason="TEST", now=at(0.5)
        )
        # Replay of the generating cycle: stored ADMITTED, but its reservation is gone.
        replay = self.cycle("c1", at(0), evaluated_at=at(0.5))
        [prop] = replay["proposals"]
        self.assertEqual((prop["decision"], prop["outcome"]), ("ADMITTED", "REPLAY"))
        self.assertIs(prop["active"], False)
        self.assertEqual(self.snap(at(0.5))["reserved_usd"], "0.00")
        again = bank.activate_admission(
            self.db,
            admission_key=q["admission_key"],
            observation_id="c1",
            observed_at=datetime.fromisoformat(q["activation_observed_at"]),
            now=at(0.5),
        )
        self.assertEqual(again["outcome"], "REPLAY")
        self.assertIs(again["active"], False)

    def test_same_identity_with_different_content_is_a_conflict_not_a_new_quote(self):
        """A new tick cannot regenerate the identity: same packet id, other content."""
        self.cycle("c1", at(0))
        [q] = self.admissions()
        bank.withdraw_admission(
            self.db, admission_key=q["admission_key"], reason="TEST", now=at(0.5)
        )
        other = self.cycle("c1", at(0.5))
        self.assertEqual(other["proposals"], [])
        self.assertIn("different proposal", other["errors"][0]["error"])
        self.assertEqual(len(self.admissions()), 1)
        self.assertEqual(self.snap(at(0.5))["reserved_usd"], "0.00")

    def test_reservation_released_out_of_band_cannot_be_activated(self):
        """`release()` is public: a reservation freed without withdrawing the admission
        must still never back an activation."""
        report = self.cycle("c1", at(0), inputs=self.inputs(at(0), fee=False))
        self.assertEqual(report["proposals"], [])
        key = "m5r1:q:manual"
        bank.admit_proposal(
            self.db,
            admission_key=key,
            origin="M5",
            thesis_id=EVENT,
            position_key=f"m5r1:{TICKER}",
            risk_cents=50,
            proposed_at=at(0),
            now=at(0),
        )
        bank.release(self.db, idempotency_key=f"adm:{key}")
        done = bank.activate_admission(
            self.db,
            admission_key=key,
            observation_id="c1",
            observed_at=at(0),
            now=at(0),
        )
        self.assertEqual(
            (done["outcome"], done["reason"]), ("NOT_ACTIVATED", "RESERVATION_NOT_ACTIVE")
        )

    def test_withdrawn_quote_cannot_fill(self):
        self.cycle("c1", at(0))
        [q] = self.admissions()
        bank.withdraw_admission(
            self.db, admission_key=q["admission_key"], reason="TEST", now=at(0.5)
        )
        report = self.cycle("c2", at(1), [self.market(BUY_CROSS, now=at(1))])
        self.assertEqual(report["evaluations"], [])
        with self.assertRaisesRegex(bank.SimulationBankValidationError, "QUOTE_WITHDRAWN"):
            bank.record_quote_observation(
                self.db,
                admission_key=q["admission_key"],
                observation_id="c2",
                observed_at=at(1),
                outcome="EVALUATED",
                fills=[],
                now=at(1),
            )


class BlockingTests(SimTestCase):
    def assert_blocked(self, report, *expected):
        self.assertEqual(report["proposals"], [])
        reasons = report["blocked"][TICKER]
        for code in expected:
            self.assertIn(code, reasons)
        self.assertEqual(self.admissions(), [])
        self.assertEqual(self.snap(at(0))["reserved_usd"], "0.00")

    def test_no_inputs_file_blocks_no_fair(self):
        self.assert_blocked(
            self.cycle("c1", at(0), inputs=None, problem="NO_INPUTS_FILE"),
            "BLOCKED_NO_FAIR",
            "NO_INPUTS_FILE",
        )

    def test_missing_fair_for_the_market_blocks(self):
        data = self.inputs(at(0), ticker="KXMLBGAME-OTHER-X")
        self.assert_blocked(self.cycle("c1", at(0), inputs=data), "BLOCKED_NO_FAIR")

    def test_stale_fair_blocks(self):
        self.assert_blocked(
            self.cycle("c1", at(0), inputs=self.inputs(at(0), fair_age=361)),
            "BLOCKED_NO_FAIR",
            "FAIR_STALE",
        )

    def test_fair_of_another_event_blocks(self):
        data = self.inputs(at(0), event="KXMLBGAME-26SEP221905OTHER")
        self.assert_blocked(self.cycle("c1", at(0), inputs=data), "FAIR_EVENT_MISMATCH")

    def test_mid_is_never_a_silent_fair(self):
        data = self.inputs(at(0))
        del data["fairs"][0]["fair_prob"]
        self.assert_blocked(self.cycle("c1", at(0), inputs=data), "INVALID_FAIR_PROB")

    def test_missing_or_stale_fee_blocks(self):
        self.assert_blocked(
            self.cycle("c1", at(0), inputs=self.inputs(at(0), fee=False)), "BLOCKED_NO_FEE"
        )

    def test_stale_fee_blocks(self):
        self.assert_blocked(
            self.cycle("c1", at(0), inputs=self.inputs(at(0), fee_age=3601)), "FEE_STALE"
        )

    def test_subpenny_book_is_incompatible_precision(self):
        book = ([["0.4550", "10"]], [["0.5000", "10"]])
        self.assert_blocked(
            self.cycle("c1", at(0), [self.market(book, now=at(0))]),
            "BLOCKED_BOOK",
            "UNSUPPORTED_LEGACY_PRECISION",
        )

    def test_fractional_depth_is_incompatible_precision(self):
        book = ([["0.4500", "1.50"]], [["0.5000", "10"]])
        self.assert_blocked(
            self.cycle("c1", at(0), [self.market(book, now=at(0))]),
            "BLOCKED_BOOK",
            "UNSUPPORTED_LEGACY_PRECISION",
        )

    def test_crossed_or_malformed_book_blocks(self):
        crossed = ([["0.5500", "10"]], [["0.5000", "10"]])
        self.assert_blocked(
            self.cycle("c1", at(0), [self.market(crossed, now=at(0))]),
            "BLOCKED_BOOK",
            "CROSSED_OR_INCOHERENT_BOOK",
        )

    def test_one_sided_book_blocks(self):
        self.assert_blocked(
            self.cycle("c1", at(0), [self.market(([], [["0.5", "1"]]), now=at(0))]),
            "BLOCKED_BOOK",
            "EMPTY_BOOK_SIDE",
        )

    def test_pregame_cutoff_blocks(self):
        data = self.inputs(at(0), kickoff=at(1))
        self.assert_blocked(self.cycle("c1", at(0), inputs=data), "PREGAME_CUTOFF")

    def test_market_not_open_blocks(self):
        self.assert_blocked(
            self.cycle("c1", at(0), [self.market(now=at(0), status="closed")]), "MARKET_NOT_OPEN"
        )

    def test_invalid_capture_blocks_everything(self):
        report = sim.run_cycle(
            bank_db=self.db,
            packet={"packet_id": "c1"},
            m1_review=None,
            inputs=self.inputs(at(0)),
            now=at(0),
        )
        self.assertEqual(report["status"], "BLOCKED_CAPTURE")
        self.assertEqual(self.admissions(), [])

    def test_review_of_another_cycle_is_not_used(self):
        packet = {
            "packet_id": "c1",
            "generated_at": at(0).isoformat(),
            "kalshi": {"markets": [self.market(now=at(0))]},
        }
        m1 = {"cycle_id": "c0", "reviews": []}
        report = sim.run_cycle(
            bank_db=self.db, packet=packet, m1_review=m1, inputs=self.inputs(at(0)), now=at(0)
        )
        self.assertEqual(report["status"], "BLOCKED_CAPTURE")

    def test_missing_bank_is_blocked_and_never_created(self):
        missing = Path(self._tmp.name) / "absent.sqlite3"
        packet = {
            "packet_id": "c1",
            "generated_at": at(0).isoformat(),
            "kalshi": {"markets": [self.market(now=at(0))]},
        }
        m1 = {"cycle_id": "c1", "reviews": []}
        report = sim.run_cycle(
            bank_db=missing, packet=packet, m1_review=m1, inputs=self.inputs(at(0)), now=at(0)
        )
        self.assertEqual(report["status"], "BLOCKED_NO_BANK")
        self.assertFalse(missing.exists())


class WithdrawalAndGapTests(SimTestCase):
    def test_ttl_withdrawal_releases_but_keeps_daily_budget(self):
        self.cycle("c1", at(0))
        report = self.cycle("c2", at(10))
        [w] = report["withdrawals"]
        self.assertEqual((w["reason"], w["reservation_released"]), ("QUOTE_TTL", True))
        # Not re-quoted in the cycle that withdrew it.
        self.assertEqual(report["proposals"], [])
        snap = self.snap(at(10))
        self.assertEqual(snap["reserved_usd"], "0.00")
        self.assertEqual(snap["today_new_risk_usd"], "0.50")
        # Next cycle quotes again: a NEW admission, and the daily budget adds up.
        self.cycle("c3", at(11))
        self.assertEqual(self.snap(at(11))["today_new_risk_usd"], "1.00")

    def test_missing_book_is_a_recorded_gap_not_a_fill_or_close(self):
        self.cycle("c1", at(0))
        report = self.cycle("c2", at(1), markets=[])
        [evaluation] = report["evaluations"]
        self.assertEqual(evaluation["status"], "GAP")
        self.assertEqual(report["withdrawals"], [])
        [q] = self.admissions()
        self.assertEqual(q["gaps"], 1)
        self.assertIs(q["reservation_active"], True)
        self.assertEqual(self.m5_events(), [])

    def test_unobserved_interval_is_flagged_uncertain(self):
        self.cycle("c1", at(0))
        report = self.cycle("c2", at(4), [self.market(BUY_CROSS, now=at(4))])
        self.assertIs(report["evaluations"][0]["uncertain"], True)

    def test_mark_jump_withdraws_after_counting_the_fill(self):
        self.cycle("c1", at(0))
        jump = ([["0.3500", "10"]], [["0.6000", "10"]])  # YES ask 40 < 45; mid moved 10
        report = self.cycle("c2", at(1), [self.market(jump, now=at(1))])
        self.assertEqual(len(report["evaluations"][0]["fills"]), 1)
        self.assertEqual(report["withdrawals"][0]["reason"], "MARK_JUMP")
        # Filled and still open → the reservation stays (conservative) until flat.
        self.assertIs(report["withdrawals"][0]["reservation_released"], False)

    def test_fair_gone_withdraws(self):
        self.cycle("c1", at(0))
        report = self.cycle("c2", at(1), inputs=None, problem="NO_INPUTS_FILE")
        self.assertEqual(report["withdrawals"][0]["reason"], "FAIR_UNAVAILABLE")


class IsolationTests(unittest.TestCase):
    def test_module_cannot_place_orders(self):
        source = (BASE / "m5_research_sim.py").read_text()
        tree = ast.parse(source)
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module)
        allowed = {
            "__future__",
            "hashlib",
            "json",
            "re",
            "dataclasses",
            "datetime",
            "fractions",
            "pathlib",
            "typing",
            "repo_root",
            "simulation_bank",
            "src.math.fees",
            "src.strategies.motor_5_mm.quoter",
            "src.strategies.motor_5_mm.shadow_fill",
        }
        self.assertLessEqual(modules, allowed, modules - allowed)
        for forbidden in ("place_order", "cancel_order", "kalshi_rest", "urllib", "socket"):
            self.assertNotIn(forbidden, source)

    def test_research_runtime_imports_without_site_packages(self):
        """The droplet runs /usr/bin/python3 with no venv: `-S` hides the dev install."""
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        done = subprocess.run(
            [sys.executable, "-S", "-c", "import research_runner, m5_research_sim"],
            cwd=BASE,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)


class RunnerWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        patcher = patch.object(research_runner.collector, "DATA_DIR", self.data)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_flag_off_writes_nothing(self):
        with patch.dict(os.environ, {"BOTKALSHI_M5_RESEARCH_ENABLED": ""}):
            research_runner._run_m5_research({"packet_id": "c1"}, None)
        self.assertFalse((self.data / "m5").exists())

    def test_failure_is_recorded_and_never_raised(self):
        with (
            patch.dict(os.environ, {"BOTKALSHI_M5_RESEARCH_ENABLED": "true"}),
            patch.object(sim, "run_cycle", side_effect=RuntimeError("boom")),
        ):
            research_runner._run_m5_research({"packet_id": "c1"}, None)
        report = json.loads((self.data / "m5" / "latest.json").read_text())
        self.assertEqual(report["status"], "ERROR")
        self.assertIs(report["execution_authorized"], False)

    def test_enabled_without_bank_reports_blocked_no_bank(self):
        packet = {"packet_id": "c1", "generated_at": T0.isoformat(), "kalshi": {"markets": []}}
        with patch.dict(os.environ, {"BOTKALSHI_M5_RESEARCH_ENABLED": "true"}):
            research_runner._run_m5_research(packet, {"cycle_id": "c1", "reviews": []})
        report = json.loads((self.data / "m5" / "latest.json").read_text())
        self.assertEqual(report["status"], "BLOCKED_NO_BANK")
        self.assertFalse((self.data / "simulation-bank.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
