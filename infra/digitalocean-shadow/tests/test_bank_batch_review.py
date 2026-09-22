"""Synthetic contract tests. No account data, provider calls or live orders."""

import copy
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
import json
from pathlib import Path
import random
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import bank_batch_review as review

NOW = datetime(2026, 9, 21, 22, 0, tzinfo=UTC)


def proposal(key="p1", origin="M1", thesis="event1", risk="1.00"):
    return {
        "proposal_id": key, "origin": origin, "thesis_id": thesis,
        "snapshot_id": "synthetic-snapshot", "observed_at": NOW.isoformat(),
        "risk_usd": risk, "includes_all_costs": True,
    }


def fixture():
    return {
        "schema_version": review.INPUT_SCHEMA,
        "snapshot_id": "synthetic-snapshot",
        "risk_day": "2026-09-21", "week_start": "2026-09-21",
        "cash_available_usd": "200.00", "unit_ceiling_usd": "2.00",
        "paused": False, "thesis_open_risk_usd": {},
        "risk": {
            "mode": "SIMULATION", "source": "confirmed-fill-ledger",
            "capital_reconciled_at": NOW.isoformat(),
            "capital_reconciled_usd": "200.00", "open_risk_usd": "0.00",
            "today_new_risk_usd": "0.00", "week_net_pnl_usd": "0.00",
            "cumulative_net_pnl_usd": "0.00",
        },
        "proposals": [proposal()],
    }


class BatchReviewTests(unittest.TestCase):
    def setUp(self):
        self.data = fixture()
        for name in ("connect", "connect_ex"):
            blocker = patch.object(socket.socket, name, side_effect=AssertionError("NETWORK FORBIDDEN"))
            blocker.start()
            self.addCleanup(blocker.stop)
        blocker = patch("socket.create_connection", side_effect=AssertionError("NETWORK FORBIDDEN"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def run_review(self):
        result = review.review_batch(self.data, now=NOW)
        for field in ("execution_authorized", "order_capability_present", "real_entry_eligible", "reservations_persisted"):
            self.assertIs(result[field], False)
        self.assertEqual(result["new_risk_headroom_usd"], "0.000000")
        return result

    def blocked(self):
        result = self.run_review()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["hypothetical_total_risk_usd"], "0.000000")

    def test_module_does_not_import_clients_or_executors(self):
        import ast
        tree = ast.parse((BASE / "bank_batch_review.py").read_text())
        allowed = {
            "__future__", "argparse", "datetime", "hashlib", "json", "os",
            "pathlib", "re", "stat", "typing", "zoneinfo", "risk_guard",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertIn(node.module, allowed)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed)

    def test_single_m1_is_hypothetical_only(self):
        result = self.run_review()
        self.assertEqual(result["hypothetical_total_risk_usd"], "1.000000")
        self.assertEqual(result["hypothetical_by_origin_usd"]["M1"], "1.000000")

    def test_m1_m5_radar_share_one_six_dollar_budget(self):
        self.data["proposals"] = [
            proposal("a", "M1", "a", "2"), proposal("b", "M5", "b", "2"),
            proposal("c", "RADAR", "c", "2"), proposal("d", "M1", "d", "1"),
        ]
        result = self.run_review()
        self.assertEqual(result["hypothetical_total_risk_usd"], "6.000000")
        self.assertIn("OPEN_CAP", result["decisions"][-1]["reasons"])
        self.assertIn("DAILY_CAP", result["decisions"][-1]["reasons"])

    def test_same_thesis_across_origins_cannot_exceed_unit(self):
        self.data["proposals"] = [proposal("a", "M1", risk="1.5"), proposal("b", "RADAR", risk="1")]
        result = self.run_review()
        self.assertEqual(result["hypothetical_total_risk_usd"], "1.500000")
        self.assertIn("THESIS_CAP", result["decisions"][1]["reasons"])

    def test_existing_manual_exposure_is_included(self):
        self.data["risk"]["open_risk_usd"] = "1.5"
        self.data["thesis_open_risk_usd"] = {"event1": "1.5"}
        self.assertEqual(self.run_review()["hypothetical_total_risk_usd"], "0.000000")

    def test_global_existing_exposure_is_included(self):
        self.data["risk"]["open_risk_usd"] = "5.5"
        self.data["thesis_open_risk_usd"] = {"a": "2", "b": "2", "c": "1.5"}
        self.assertIn("OPEN_CAP", self.run_review()["decisions"][0]["reasons"])

    def test_exhausted_daily_budget_is_not_recycled(self):
        self.data["risk"]["today_new_risk_usd"] = "6"
        self.assertIn("DAILY_CAP", self.run_review()["decisions"][0]["reasons"])

    def test_cash_can_block_even_with_risk_capacity(self):
        self.data["cash_available_usd"] = "0.999999"
        self.assertIn("CASH_CAP", self.run_review()["decisions"][0]["reasons"])

    def test_experiment_room_counts_open_risk(self):
        self.data["risk"]["cumulative_net_pnl_usd"] = "-18.5"
        self.data["risk"]["open_risk_usd"] = "0.75"
        self.data["thesis_open_risk_usd"] = {"other": "0.75"}
        self.assertIn("EXPERIMENT_CAP", self.run_review()["decisions"][0]["reasons"])

    def test_weekly_stop(self):
        self.data["risk"]["week_net_pnl_usd"] = "-12"
        self.assertIn("PAUSED", self.run_review()["decisions"][0]["reasons"])

    def test_experiment_stop(self):
        self.data["risk"]["cumulative_net_pnl_usd"] = "-20"
        self.assertIn("PAUSED", self.run_review()["decisions"][0]["reasons"])

    def test_explicit_pause_survives_positive_pnl(self):
        self.data["paused"] = True
        self.data["risk"]["cumulative_net_pnl_usd"] = "100"
        self.assertIn("PAUSED", self.run_review()["decisions"][0]["reasons"])

    def test_cash_deposit_does_not_erase_loss_stop(self):
        self.data["risk"].update(capital_reconciled_usd="500", cumulative_net_pnl_usd="-20")
        self.data["cash_available_usd"] = "500"
        self.assertIn("PAUSED", self.run_review()["decisions"][0]["reasons"])

    def test_unit_ceiling_does_not_grow_with_capital(self):
        self.data["risk"]["capital_reconciled_usd"] = "400"
        self.data["unit_ceiling_usd"] = "1.80"
        self.assertEqual(self.run_review()["unit_usd"], "1.800000")

    def test_unit_reduces_on_capital_drop(self):
        self.data["risk"]["capital_reconciled_usd"] = "180"
        self.data["cash_available_usd"] = "180"
        self.assertEqual(self.run_review()["unit_usd"], "1.800000")

    def test_money_precision_does_not_round_risk_down(self):
        self.data["proposals"] = [proposal(risk="2.000001")]
        self.assertIn("THESIS_CAP", self.run_review()["decisions"][0]["reasons"])

    def test_subcent_requests_remain_exact(self):
        self.data["proposals"] = [proposal(risk="0.000001")]
        self.assertEqual(self.run_review()["hypothetical_total_risk_usd"], "0.000001")

    def test_identical_repeated_id_is_counted_once(self):
        self.data["proposals"].append(copy.deepcopy(self.data["proposals"][0]))
        result = self.run_review()
        self.assertEqual(result["duplicates_ignored"], 1)
        self.assertEqual(len(result["decisions"]), 1)

    def test_dedupe_normalizes_decimal_strings(self):
        self.data["proposals"].append(proposal(risk="1"))
        self.assertEqual(self.run_review()["duplicates_ignored"], 1)

    def test_conflicting_duplicate_blocks_whole_batch(self):
        self.data["proposals"].append(proposal(risk="2"))
        self.blocked()

    def test_different_origin_same_id_requires_reconciliation(self):
        self.data["proposals"].append(proposal(origin="RADAR"))
        self.blocked()

    def test_late_invalid_row_does_not_partially_accept_batch(self):
        self.data["proposals"].append(proposal("bad", origin="UNKNOWN"))
        self.blocked()

    def test_unknown_exposure_is_not_zero(self):
        self.data["risk"]["open_risk_usd"] = None
        self.blocked()

    def test_incomplete_thesis_breakdown_blocks(self):
        self.data["risk"]["open_risk_usd"] = "1"
        self.blocked()

    def test_mixed_snapshot_blocks(self):
        self.data["proposals"][0]["snapshot_id"] = "other"
        self.blocked()

    def test_stale_future_naive_extreme_dates_block(self):
        for stamp in ("banana", "2026-09-21T21:56:59Z", "2026-09-21T22:00:01Z", "2026-09-21T22:00:00", "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            for location in ("risk", "proposal"):
                with self.subTest(stamp=stamp, location=location):
                    self.data = fixture()
                    target, key = (self.data["risk"], "capital_reconciled_at") if location == "risk" else (self.data["proposals"][0], "observed_at")
                    target[key] = stamp
                    self.blocked()

    def test_boundary_age_is_allowed_but_not_execution(self):
        self.data["risk"]["capital_reconciled_at"] = (NOW - timedelta(seconds=180)).isoformat()
        self.assertEqual(self.run_review()["status"], "REVIEW_ONLY")

    def test_wrong_local_day_or_week_blocks(self):
        for key in ("risk_day", "week_start"):
            with self.subTest(key=key):
                self.data = fixture()
                self.data[key] = "2026-09-20"
                self.blocked()

    def test_utc_date_is_not_used_as_local_day(self):
        clock = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
        self.data["risk"]["capital_reconciled_at"] = clock.isoformat()
        self.data["proposals"][0]["observed_at"] = clock.isoformat()
        self.assertEqual(review.review_batch(self.data, now=clock)["status"], "REVIEW_ONLY")

    def test_authority_cannot_be_granted_even_with_truthy_one(self):
        for key in review.AUTHORITY_FIELDS:
            for value in (True, 1, 0, "false", None):
                with self.subTest(key=key, value=value):
                    self.data = fixture()
                    self.data["risk"][key] = value
                    self.blocked()

    def test_real_label_never_means_verified_or_authorized(self):
        self.data["risk"]["mode"] = "REAL_SEPARATED"
        self.assertEqual(self.run_review()["evidence_state"], "DECLARATIVE_NOT_VERIFIED")

    def test_invalid_money_values_block(self):
        for value in (None, True, 1.25, 1, "NaN", "Infinity", "1e999999", "-1", "0.0000001", "1000000000", " 1", "+1", [], {}):
            with self.subTest(value=value):
                self.data = fixture()
                self.data["cash_available_usd"] = value
                self.blocked()

    def test_zero_risk_and_fees_unknown_block(self):
        for field, value in (("risk_usd", "0"), ("includes_all_costs", False), ("includes_all_costs", 1)):
            self.data = fixture()
            self.data["proposals"][0][field] = value
            self.blocked()

    def test_cash_unit_pause_and_source_validation(self):
        for key, value in (("cash_available_usd", "201"), ("unit_ceiling_usd", "2.01"), ("unit_ceiling_usd", "0"), ("unit_ceiling_usd", "1.001"), ("paused", 0)):
            self.data = fixture()
            self.data[key] = value
            self.blocked()
        for key, value in (("source", "trust-me"), ("source", []), ("mode", {})):
            self.data = fixture()
            self.data["risk"][key] = value
            self.blocked()

    def test_unknown_missing_and_extra_fields_block(self):
        self.data["order"] = "BUY"
        self.blocked()
        self.data = fixture()
        del self.data["cash_available_usd"]
        self.blocked()
        for value in (None, [], "", True):
            self.data = value
            self.blocked()

    def test_batch_row_limit(self):
        self.data["proposals"] = [proposal(str(i), thesis=str(i)) for i in range(101)]
        self.blocked()

    def test_empty_batch_is_review_only(self):
        self.data["proposals"] = []
        self.assertEqual(self.run_review()["hypothetical_total_risk_usd"], "0.000000")

    def test_no_mutation_and_no_hidden_persistence(self):
        before = copy.deepcopy(self.data)
        first = self.run_review()
        second = self.run_review()
        self.assertEqual(self.data, before)
        self.assertEqual(first, second)
        self.assertIs(first["reservations_persisted"], False)

    def test_clock_must_be_aware(self):
        for clock in (NOW.replace(tzinfo=None), "now", datetime.min.replace(tzinfo=UTC)):
            self.assertEqual(review.review_batch(self.data, now=clock)["status"], "BLOCKED")

    def test_input_hash_changes_on_evidence_change(self):
        first = self.run_review()["input_sha256"]
        self.data["cash_available_usd"] = "199"
        self.assertNotEqual(self.run_review()["input_sha256"], first)

    def test_randomized_global_and_thesis_invariants(self):
        rng = random.Random(256)
        for case in range(500):
            self.data = fixture()
            cash = rng.randrange(0, 10 * review.MICRO)
            daily = rng.randrange(0, 6 * review.MICRO)
            self.data["cash_available_usd"] = review._usd(cash)
            self.data["risk"]["today_new_risk_usd"] = review._usd(daily)
            self.data["proposals"] = [proposal(str(i), rng.choice(sorted(review.ORIGINS)), f"event{rng.randrange(4)}", review._usd(rng.randrange(1, 3 * review.MICRO))) for i in range(20)]
            result = self.run_review()
            self.assertEqual(result["status"], "REVIEW_ONLY", case)
            total = review._amount(result["hypothetical_total_risk_usd"])
            self.assertLessEqual(total, min(cash, 6 * review.MICRO - daily))
            by_thesis = {}
            for row in result["decisions"]:
                by_thesis[row["thesis_id"]] = by_thesis.get(row["thesis_id"], 0) + review._amount(row["hypothetical_risk_usd"])
            self.assertTrue(all(value <= 2 * review.MICRO for value in by_thesis.values()))

    def test_bounded_file_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text(json.dumps(self.data))
            self.assertEqual(review.read_input(path), self.data)
            path.write_text(" " * (review.MAX_BYTES + 1))
            with self.assertRaises(review.BatchInputError):
                review.read_input(path)

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text('{"paused": true, "paused": false}')
            with self.assertRaises(review.BatchInputError):
                review.read_input(path)

    def test_symlink_and_fifo_rejected(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text("{}")
            link = Path(tmp) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                review.read_input(link)
            fifo = Path(tmp) / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(review.BatchInputError):
                review.read_input(fifo)

    def test_cli_success_and_failure_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text(json.dumps(self.data))
            original = review.review_batch
            with patch.object(sys, "argv", ["review", "--input", str(path)]), patch.object(review, "review_batch", side_effect=lambda data: original(data, now=NOW)), redirect_stdout(StringIO()) as output:
                self.assertEqual(review.main(), 0)
                self.assertFalse(json.loads(output.getvalue())["execution_authorized"])
            path.write_text("not-json")
            with patch.object(sys, "argv", ["review", "--input", str(path)]), redirect_stdout(StringIO()) as output:
                self.assertEqual(review.main(), 2)
                self.assertEqual(json.loads(output.getvalue())["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
