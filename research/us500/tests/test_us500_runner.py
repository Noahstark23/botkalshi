import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from example_session import session
from us500_engine import DataError, body_digest
from us500_ledger import PaperLedger
from us500_runner import build_plan, replay


def rehash(frame):
    for obs in frame["snapshot"]["observations"].values():
        obs["canonical_body_sha256"] = body_digest(obs["body"])


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch(
            "socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN")
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "paper.db"

    def test_next_observation_execution_and_final_flat_costed_result(self):
        events, result = build_plan(session())
        fills = [e for e in events if e["type"] == "fill"]
        self.assertTrue(fills)
        self.assertEqual(fills[0]["at_ms"], session()["frames"][2]["at_ms"])
        self.assertEqual(result["residual_position_quantity"], "0")
        self.assertLess(Decimal(result["final_state"]["cash_usd"]), 100)
        self.assertGreater(Decimal(result["final_state"]["fees_usd"]), 0)
        self.assertEqual(Decimal(result["final_state"]["costs_usd"]), Decimal("0.01"))
        self.assertFalse(result["strategy_validated"])
        self.assertEqual(result["actual_orders"], 0)

    def test_input_prefix_does_not_use_future_data(self):
        a = session()
        b = copy.deepcopy(a)
        b["frames"] = b["frames"][:5]
        full_events, full = build_plan(a)
        partial_events, partial = build_plan(b)
        self.assertEqual(full_events[: len(partial_events)], partial_events)
        self.assertEqual(full["frames"][:5], partial["frames"])

    def test_restart_is_idempotent_and_append_replays_identically(self):
        data = session()
        prefix = copy.deepcopy(data)
        prefix["frames"] = prefix["frames"][:4]
        replay(prefix, self.db)
        first = replay(data, self.db)
        second = replay(data, self.db)
        self.assertEqual(first, second)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM ledger_events").fetchone()[0],
                first["ledger_events"],
            )

    def test_crash_between_commits_recovers_without_duplicate_money(self):
        apply = PaperLedger.apply
        seen = 0

        def fault(book, event):
            nonlocal seen
            disk = book._db.execute("PRAGMA database_list").fetchone()[2]
            if disk:
                seen += 1
                if seen == 10:
                    raise RuntimeError("INJECTED_CRASH")
            return apply(book, event)

        with (
            patch.object(PaperLedger, "apply", fault),
            self.assertRaisesRegex(RuntimeError, "INJECTED_CRASH"),
        ):
            replay(session(), self.db)
        result = replay(session(), self.db)
        _, expected = build_plan(session())
        self.assertEqual(result["final_state"], expected["final_state"])

    def test_changed_history_or_config_rejects_without_cash_mutation(self):
        original = session()
        first = replay(original, self.db)
        for changed in ("history", "config", "truncation"):
            data = copy.deepcopy(original)
            if changed == "history":
                data["frames"][0]["infrastructure_cost_usd"] = "1"
            elif changed == "config":
                data["strategy"]["threshold_bps"] = "20"
            else:
                data["frames"].pop()
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(DataError, "SESSION_INPUT_CHANGED_OR_TRUNCATED"),
            ):
                replay(data, self.db)
        self.assertEqual(replay(original, self.db), first)

    def test_unbounded_settlement_price_rejects_before_creating_ledger(self):
        for price in ("1000000000", "1e1000"):
            data = session()
            data["frames"][2]["variation_margin_settlement_price"] = price
            with self.subTest(price=price), self.assertRaises((DataError, ValueError)):
                replay(data, self.db)
            self.assertFalse(self.db.exists())
        data = session()
        data["frames"][2]["variation_margin_settlement_price"] = "999999999.9999"
        events, _ = build_plan(data)
        self.assertTrue(any(e["type"] == "variation_margin" for e in events))

    def test_partial_depth_never_invents_a_full_fill(self):
        data = session(depth="1")
        events, result = build_plan(data)
        self.assertTrue(
            any(r["status"] == "HYPOTHETICAL_PARTIAL_IOC" for r in result["frames"])
        )
        self.assertTrue(any(e["type"] == "cancel" for e in events))
        self.assertTrue(
            all(Decimal(e["quantity"]) <= 1 for e in events if e["type"] == "fill")
        )
        self.assertEqual(result["final_state"]["reserved_margin_usd"], "0")

    def test_strict_notional_cap_rejects_entries_durably(self):
        data = session()
        data["ledger"]["max_notional_usd"] = "1"
        events, result = build_plan(data)
        self.assertFalse(any(e["type"] == "fill" for e in events))
        self.assertTrue(
            any(r["status"].startswith("RISK_REJECTED") for r in result["frames"])
        )

    def test_bad_market_data_stops_without_hypothetical_execution(self):
        for key, value in (("status", "inactive"), ("schedule", {"is_open": False})):
            data = session()
            frame = data["frames"][2]
            frame["snapshot"]["observations"]["market"]["body"]["market"][key] = value
            rehash(frame)
            events, result = build_plan(data)
            self.assertEqual(result["frames"][-1]["status"], "DATA_BLOCKED")
            self.assertFalse(any(e["type"] == "fill" for e in events))
            self.assertTrue(result["final_state"]["paused"])

    def test_margin_mark_separate_from_mid_and_not_required_on_tick(self):
        data = session()
        for frame in data["frames"]:
            frame["snapshot"]["observations"]["market"]["body"]["market"][
                "liquidation_mark_price"
            ]["price"] = "13.00005"
            rehash(frame)
        _, result = build_plan(data)
        self.assertEqual(result["final_state"]["mark_price"], "13.00005")

    def test_missing_stale_mark_and_tampered_digest_block(self):
        for change in ("missing", "stale", "digest"):
            data = session()
            f = data["frames"][0]
            m = f["snapshot"]["observations"]["market"]["body"]["market"]
            if change == "missing":
                del m["liquidation_mark_price"]
            elif change == "stale":
                m["liquidation_mark_price"]["ts_ms"] -= 6000
            else:
                m["tick_size"] = "0.01"
            if change != "digest":
                rehash(f)
            _, result = build_plan(data)
            self.assertEqual(result["frames"][0]["status"], "DATA_BLOCKED")
            self.assertFalse(
                result["economic_path_complete_under_explicit_assumptions"]
            )

    def test_gap_with_open_position_preserves_residual_and_pauses(self):
        data = session()
        # Move an entire suffix; retain internally consistent snapshot clocks.
        for f in data["frames"][3:]:
            f["at_ms"] += 10000
            for obs in f["snapshot"]["observations"].values():
                obs["requested_at_ms"] += 10000
                obs["received_at_ms"] += 10000
            market = f["snapshot"]["observations"]["market"]["body"]["market"]
            for key in ("reference_price", "liquidation_mark_price"):
                market[key]["ts_ms"] += 10000
            rehash(f)
        _, result = build_plan(data)
        self.assertTrue(result["final_state"]["paused"])
        self.assertNotEqual(Decimal(result["residual_position_quantity"]), 0)
        self.assertFalse(result["economic_path_complete_under_explicit_assumptions"])

    def test_funding_cannot_create_profit_without_a_position(self):
        data = session()
        data["frames"][0]["funding_cashflow_usd"] = "100"
        with self.assertRaisesRegex(DataError, "FUNDING_WITHOUT_POSITION"):
            replay(data, self.db)
        self.assertFalse(self.db.exists())

    def test_funding_debit_is_included_once_and_survives_replay(self):
        data = session()
        data["frames"][3]["funding_cashflow_usd"] = "-0.1"
        report = replay(data, self.db)
        self.assertEqual(Decimal(report["final_state"]["funding_usd"]), Decimal("-0.1"))
        self.assertEqual(report, replay(data, self.db))

    def test_reusing_signal_capture_cannot_supply_future_liquidity(self):
        data = session()
        data["frames"][2]["snapshot"] = copy.deepcopy(data["frames"][1]["snapshot"])
        events, report = build_plan(data)
        self.assertEqual(
            report["frames"][-1]["reason"], "REUSED_OR_OVERLAPPING_CAPTURE"
        )
        self.assertFalse(any(e["type"] == "fill" for e in events))

    def test_all_interval_costs_are_required(self):
        for key in ("funding_cashflow_usd", "infrastructure_cost_usd"):
            data = session()
            del data["frames"][0][key]
            with self.assertRaisesRegex(DataError, "EXPLICIT_INTERVAL_COSTS_REQUIRED"):
                replay(data, self.db)
            self.assertFalse(self.db.exists())

    def test_foreign_database_not_modified(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE historical (v TEXT)")
            db.execute("INSERT INTO historical VALUES ('untouched')")
            db.commit()
        original = self.db.read_bytes()
        with self.assertRaisesRegex(DataError, "DATABASE_NOT_OWNED"):
            replay(session(), self.db)
        self.assertEqual(self.db.read_bytes(), original)

    def test_symlink_database_rejected(self):
        target = Path(self.temp.name) / "target"
        target.write_text("untouched")
        self.db.symlink_to(target)
        with self.assertRaises(DataError):
            replay(session(), self.db)
        self.assertEqual(target.read_text(), "untouched")

    def test_risk_breach_on_last_fill_is_not_a_complete_economic_path(self):
        data = session(prices=["13", "13.1", "13.2"])
        data["ledger"]["max_loss_usd"] = "0.05"
        _, report = build_plan(data)
        self.assertTrue(report["final_state"]["paused"])
        self.assertFalse(report["economic_path_complete_under_explicit_assumptions"])

    def test_atomic_manifest_initialization_survives_interrupted_publication(self):
        real_link = __import__("os").link
        with patch(
            "us500_runner.os.link", side_effect=OSError("INJECTED_INIT_FAILURE")
        ):
            with self.assertRaises(OSError):
                replay(session(), self.db)
        self.assertFalse(self.db.exists())
        self.assertEqual(list(Path(self.temp.name).glob(".us500-init-*")), [])
        self.assertTrue(callable(real_link))
        self.assertTrue(replay(session(), self.db)["durable_replay_verified"])

    def test_final_open_position_is_reported_without_inventing_close(self):
        data = session(prices=["13", "13.1", "13.2"])
        _, report = build_plan(data)
        self.assertEqual(Decimal(report["residual_position_quantity"]), 2)
        self.assertEqual(report["independent_settled_samples"], 0)

    def test_flat_market_only_costs_without_spurious_trades(self):
        events, report = build_plan(session(prices=["13"] * 6))
        self.assertFalse(any(e["type"] == "fill" for e in events))
        self.assertEqual(Decimal(report["final_state"]["cash_usd"]), Decimal("99.994"))

    def test_no_binary_float_in_saved_report(self):
        _, report = build_plan(session())
        parsed = json.loads(json.dumps(report))
        self.assertIsInstance(parsed["final_state"]["cash_usd"], str)

    def test_fractional_quantity_finer_than_api_rejected_by_core(self):
        from test_us500_engine import sample, scenario

        a, b = sample(), sample(at=1791744850000)
        for frame in (a, b):
            frame["observations"]["market"]["body"]["market"][
                "fractional_trading_enabled"
            ] = True
        with self.assertRaisesRegex(DataError, "OFF_API_QUANTITY_STEP"):
            scenario(a, b, quantity="0.001")


if __name__ == "__main__":
    unittest.main()
