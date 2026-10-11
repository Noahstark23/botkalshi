import copy
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from example_session import session
from us500_control import PaperControl
from us500_engine import DataError
from us500_ledger import PaperLedger
from us500_runner import build_plan, replay

AT = 1791744840000


def callback(uid, data):
    return {
        "update_id": uid,
        "callback_query": {
            "id": "query-" + str(uid),
            "from": {"id": 11, "is_bot": False},
            "message": {
                "message_id": 1,
                "date": AT // 1000,
                "chat": {"id": 11, "type": "private"},
            },
            "data": data,
        },
    }


def enable(control, uid=1):
    answers = control.handle_update(callback(uid, "u5:enable"), now_ms=AT + uid)
    confirmation = next(
        a["payload"]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        for a in answers
        if a["method"] == "sendMessage"
    )
    control.handle_update(callback(uid + 1, confirmation), now_ms=AT + uid + 1)
    assert control.snapshot()["paper_enabled"]


class ControlledRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "paper.sqlite3"
        self.control = PaperControl(
            self.root / "control.sqlite3", owner_user_id=11, owner_chat_id=11
        )

    def test_disabled_gate_does_not_create_ledger(self):
        report = replay(session(), self.db, control=self.control)
        self.assertEqual(report["status"], "PAPER_DISABLED")
        self.assertFalse(self.db.exists())

    def test_omitting_control_cannot_bypass_pause_of_bound_ledger(self):
        enable(self.control)
        data = session()
        prefix = copy.deepcopy(data)
        prefix["frames"] = prefix["frames"][:3]
        replay(prefix, self.db, control=self.control)
        self.control.handle_update(callback(3, "u5:pause"), now_ms=AT + 3)
        before = self.db.read_bytes()
        with self.assertRaisesRegex(DataError, "BOUND_PAPER_CONTROL_REQUIRED"):
            replay(data, self.db)
        self.assertEqual(before, self.db.read_bytes())

    def test_prior_model_database_is_not_mutated_or_silently_migrated(self):
        replay(session(), self.db)
        db = sqlite3.connect(self.db)
        with db:
            old = json.loads(
                db.execute("SELECT metadata FROM us500_runner_manifest").fetchone()[0]
            )
            old.pop("_runner_model_revision")
            db.execute(
                "UPDATE us500_runner_manifest SET metadata=?", (json.dumps(old),)
            )
        db.close()
        before = self.db.read_bytes()
        with self.assertRaisesRegex(DataError, "MODEL_VERSION_MISMATCH"):
            replay(session(), self.db)
        self.assertEqual(before, self.db.read_bytes())

    def test_confirmed_gate_replays_same_money_after_restart(self):
        enable(self.control)
        first = replay(session(), self.db, control=self.control)
        restarted = PaperControl(self.control.path, owner_user_id=11, owner_chat_id=11)
        second = replay(session(), self.db, control=restarted)
        self.assertEqual(first, second)
        self.assertEqual(first["final_state"], build_plan(session())[1]["final_state"])

    def test_pause_after_reserve_keeps_money_and_resume_is_idempotent(self):
        enable(self.control)
        original_apply = PaperLedger.apply
        paused = False
        should_pause = False
        original_permit = self.control.with_permit

        def observe(book, event):
            nonlocal should_pause
            result = original_apply(book, event)
            if (
                book._db.execute("PRAGMA database_list").fetchone()[2]
                and event["type"] == "reserve"
            ):
                should_pause = True
            return result

        def permit(action):
            nonlocal paused
            result = original_permit(action)
            if should_pause and not paused:
                paused = True
                self.control.handle_update(callback(3, "u5:pause"), now_ms=AT + 3)
            return result

        with (
            patch.object(PaperLedger, "apply", observe),
            patch.object(self.control, "with_permit", permit),
        ):
            interrupted = replay(session(), self.db, control=self.control)
        self.assertEqual(interrupted["status"], "OPERATOR_PAUSED")
        state = interrupted["final_state"]
        self.assertEqual(Decimal(state["position_quantity"]), 0)
        self.assertGreater(Decimal(state["reserved_margin_usd"]), 0)
        enable(self.control, 4)
        finished = replay(session(), self.db, control=self.control)
        self.assertEqual(
            finished["final_state"], build_plan(session())[1]["final_state"]
        )

    def test_enable_does_not_clear_ledger_risk_pause(self):
        data = session(prices=["13", "13.1", "13.2"])
        data["ledger"]["max_loss_usd"] = "0.05"
        enable(self.control)
        first = replay(data, self.db, control=self.control)
        self.assertTrue(first["final_state"]["paused"])
        self.control.handle_update(callback(3, "u5:pause"), now_ms=AT + 3)
        enable(self.control, 4)
        self.assertEqual(
            first["final_state"],
            replay(data, self.db, control=self.control)["final_state"],
        )

    def test_control_and_ledger_cannot_share_database(self):
        with self.assertRaisesRegex(DataError, "MUST_DIFFER"):
            replay(session(), self.control.path, control=self.control)

    def test_a_different_control_identity_cannot_take_ledger(self):
        enable(self.control)
        replay(session(), self.db, control=self.control)
        another = PaperControl(
            self.root / "other.db", owner_user_id=11, owner_chat_id=11
        )
        enable(another)
        with self.assertRaisesRegex(DataError, "BINDING_CONFLICT"):
            replay(session(), self.db, control=another)

    def test_variation_settlement_keeps_total_pnl_and_restart(self):
        data = session()
        baseline = build_plan(data)[1]
        data["frames"][3]["variation_margin_settlement_price"] = "13.05"
        events, report = build_plan(data)
        self.assertTrue(any(e["type"] == "variation_margin" for e in events))
        self.assertEqual(
            Decimal(report["final_state"]["net_pnl_usd"]),
            Decimal(baseline["final_state"]["net_pnl_usd"]),
        )
        self.assertNotEqual(Decimal(report["final_state"]["variation_margin_usd"]), 0)
        self.assertEqual(replay(data, self.db), replay(data, self.db))

    def test_margin_rates_emit_before_hypothetical_orders_and_persist(self):
        data = session()
        data["frames"][2]["margin_rates"] = {
            "initial_margin_rate": "0.9",
            "maintenance_margin_rate": "0.4",
        }
        events, report = build_plan(data)
        kinds = [e["type"] for e in events if e["at_ms"] == data["frames"][2]["at_ms"]]
        self.assertLess(kinds.index("margin_rates"), kinds.index("reserve"))
        self.assertEqual(
            Decimal(report["final_state"]["initial_margin_rate"]), Decimal("0.9")
        )
        self.assertEqual(replay(data, self.db), replay(data, self.db))

    def test_bad_rates_rejected_before_persistent_mutation(self):
        for rates in (
            {},
            {"initial_margin_rate": "0.2", "maintenance_margin_rate": "0.5"},
            {"initial_margin_rate": "1.1", "maintenance_margin_rate": "0.5"},
        ):
            data = copy.deepcopy(session())
            data["frames"][3]["margin_rates"] = rates
            with self.assertRaises(DataError):
                replay(data, self.db)
            self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
