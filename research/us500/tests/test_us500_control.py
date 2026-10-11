"""Offline Telegram control tests: no token, transport, service or real orders."""

import copy
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from us500_control import ControlError, PaperControl

NOW = 1791744840000
OWNER = 10101
CHAT = 20202


def update(
    number, *, action=None, text="/us500", actor=OWNER, chat=CHAT, kind="private"
):
    person = {"id": actor, "is_bot": False}
    message = {"message_id": 1, "date": 1791744840, "chat": {"id": chat, "type": kind}}
    if action is None:
        message.update({"from": person, "text": text})
        return {"update_id": number, "message": message}
    return {
        "update_id": number,
        "callback_query": {
            "id": f"query-{number}",
            "from": person,
            "message": message,
            "data": action,
        },
    }


def confirmation(responses):
    return responses[-1]["payload"]["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch(
            "socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN")
        )
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "control.sqlite"
        self.control = PaperControl(self.path, owner_user_id=OWNER, owner_chat_id=CHAT)

    def send(self, number, *, at=None, **kwargs):
        return self.control.handle_update(
            update(number, **kwargs), now_ms=at or NOW + number
        )

    def enable(self, start=1):
        action = confirmation(self.send(start, action="u5:enable"))
        self.send(start + 1, action=action)

    def test_default_disabled_and_permit_does_not_run_callback(self):
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        calls = []
        result = self.control.with_permit(lambda state: calls.append(state))
        self.assertEqual(result["reason"], "PAPER_DISABLED")
        self.assertFalse(result["permitted"])
        self.assertEqual(calls, [])

    def test_commands_offer_only_paper_controls(self):
        for number, text in enumerate(("/motores", "/us500"), 1):
            responses = self.send(number, text=text)
            self.assertEqual(responses[0]["method"], "sendMessage")
            self.assertIn("simulación", responses[0]["payload"]["text"])
            buttons = responses[0]["payload"]["reply_markup"]["inline_keyboard"]
            self.assertEqual(
                [b["callback_data"] for row in buttons for b in row],
                ["u5:enable", "u5:pause", "u5:status"],
            )
        self.assertFalse(self.control.snapshot()["production_control_available"])

    def test_exact_owner_and_private_chat_required_without_state_changes(self):
        original = self.control.snapshot()
        variants = [
            dict(actor=OWNER + 1),
            dict(chat=CHAT + 1),
            dict(kind="group"),
            dict(kind="supergroup"),
            dict(kind="channel"),
            dict(actor=True),
            dict(chat=True),
        ]
        for kwargs in variants:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.send(1, action="u5:pause", **kwargs), [])
                self.assertEqual(self.control.snapshot(), original)

    def test_bot_forward_channel_and_inline_callbacks_are_ignored(self):
        original = self.control.snapshot()
        variants = []
        for field in (
            "forward_origin",
            "forward_from",
            "forward_from_chat",
            "forward_date",
            "sender_chat",
            "via_bot",
        ):
            item = update(1)
            item["message"][field] = {"untrusted": True}
            variants.append(item)
        item = update(1)
        item["message"]["from"]["is_bot"] = True
        variants.append(item)
        item = update(1, action="u5:enable")
        item["callback_query"]["inline_message_id"] = "inline"
        variants.append(item)
        item = update(1, action="u5:enable")
        item["callback_query"]["from"]["is_bot"] = True
        variants.append(item)
        item = update(1, action="u5:enable")
        item["callback_query"]["message"]["date"] = 0
        variants.append(item)
        for item in variants:
            self.assertEqual(self.control.handle_update(item, now_ms=NOW), [])
        self.assertEqual(self.control.snapshot(), original)

    def test_activation_requires_one_time_confirmation_and_survives_restart(self):
        responses = self.send(1, action="u5:enable")
        self.assertEqual(responses[0]["method"], "answerCallbackQuery")
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        action = confirmation(responses)
        self.send(2, action=action)
        active = self.control.snapshot()
        self.assertTrue(active["paper_enabled"])
        self.assertEqual(active["revision"], 1)
        self.assertFalse(active["ledger_risk_pause_cleared"])
        reopened = PaperControl(self.path, owner_user_id=OWNER, owner_chat_id=CHAT)
        self.assertEqual(reopened.snapshot(), active)
        self.send(3, action=action)
        self.assertEqual(self.control.snapshot()["revision"], 1)

    def test_confirmation_bound_to_actor_and_chat(self):
        action = confirmation(self.send(1, action="u5:enable"))
        self.assertEqual(self.send(2, action=action, actor=OWNER + 1), [])
        self.assertEqual(self.send(2, action=action, chat=CHAT + 1), [])
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.send(2, action=action)
        self.assertTrue(self.control.snapshot()["paper_enabled"])

    def test_expiry_boundary_rejects_confirmation(self):
        action = confirmation(self.send(1, action="u5:enable", at=NOW))
        responses = self.send(2, action=action, at=NOW + 60000)
        self.assertIn("vencida", responses[-1]["payload"]["text"])
        self.assertFalse(self.control.snapshot()["paper_enabled"])

    def test_pause_invalidates_pending_confirmations_and_is_durable(self):
        action = confirmation(self.send(1, action="u5:enable"))
        result = self.send(2, action="u5:pause")
        self.assertIn("reservas y posiciones", result[-1]["payload"]["text"])
        self.send(3, action=action)
        state = PaperControl(
            self.path, owner_user_id=OWNER, owner_chat_id=CHAT
        ).snapshot()
        self.assertFalse(state["paper_enabled"])
        self.assertEqual(state["reason"], "OWNER_PAUSED")
        self.assertEqual(state["revision"], 1)

    def test_new_confirmation_invalidates_previous_without_enabling(self):
        first = confirmation(self.send(1, action="u5:enable"))
        second = confirmation(self.send(2, action="u5:enable"))
        self.assertNotEqual(first, second)
        self.send(3, action=first)
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.send(4, action=second)
        self.assertTrue(self.control.snapshot()["paper_enabled"])

    def test_idempotent_update_replay_and_conflict(self):
        item = update(1, action="u5:enable")
        first = self.control.handle_update(item, now_ms=NOW)
        state = self.control.snapshot()
        self.assertEqual(self.control.handle_update(item, now_ms=NOW + 1), first)
        self.assertEqual(self.control.snapshot(), state)
        altered = update(1, action="u5:pause")
        with self.assertRaisesRegex(ControlError, "UPDATE_ID_CONFLICT"):
            self.control.handle_update(altered, now_ms=NOW + 2)
        self.assertEqual(self.control.snapshot(), state)

    def test_out_of_order_callback_has_no_effect(self):
        action = confirmation(self.send(10, action="u5:enable"))
        self.send(12, action="u5:status")
        state = self.control.snapshot()
        self.assertEqual(self.send(11, action=action), [])
        self.assertEqual(self.control.snapshot(), state)

    def test_enabled_permit_callback_and_pause_excludes_future_work(self):
        self.enable()
        permitted = self.control.with_permit(
            lambda state: {"revision_seen": state["revision"]}
        )
        self.assertTrue(permitted["permitted"])
        self.assertEqual(permitted["result"], {"revision_seen": 1})
        self.send(3, action="u5:pause")
        self.assertFalse(
            self.control.with_permit(lambda _: self.fail("must not run"))["permitted"]
        )

    def test_concurrent_pause_waits_for_permitted_frame_then_blocks_next(self):
        self._assert_concurrent_pause(lambda: self.send(3, action="u5:pause"))

    def test_concurrent_fault_pause_waits_for_permitted_frame_then_blocks_next(self):
        self._assert_concurrent_pause(
            lambda: self.control.pause_for_fault("TELEGRAM_UNAVAILABLE", now_ms=NOW + 3)
        )

    def _assert_concurrent_pause(self, pause):
        self.enable()
        entered, release, paused = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        results, failures = [], []

        def frame(state):
            entered.set()
            if not release.wait(3):
                raise AssertionError("FRAME_TEST_TIMEOUT")
            return state["revision"]

        def permit_worker():
            try:
                results.append(self.control.with_permit(frame))
            except Exception as exc:
                failures.append(exc)

        def pause_worker():
            try:
                pause()
                paused.set()
            except Exception as exc:
                failures.append(exc)

        a = threading.Thread(target=permit_worker)
        b = threading.Thread(target=pause_worker)
        a.start()
        self.assertTrue(entered.wait(2))
        b.start()
        self.assertFalse(paused.wait(0.05))
        release.set()
        a.join(3)
        b.join(3)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(paused.is_set())
        self.assertTrue(results[0]["permitted"])
        self.assertFalse(self.control.snapshot()["paper_enabled"])

    def test_callback_error_releases_lock_and_does_not_change_control(self):
        self.enable()
        state = self.control.snapshot()

        def fail(_):
            raise RuntimeError("LOCAL_PAPER_FAILURE")

        with self.assertRaisesRegex(RuntimeError, "LOCAL_PAPER_FAILURE"):
            self.control.with_permit(fail)
        self.assertEqual(self.control.snapshot(), state)
        self.send(3, action="u5:pause")
        self.assertFalse(self.control.snapshot()["paper_enabled"])

    def test_tables_are_bounded_old_update_cannot_reexecute(self):
        path = Path(self.temp.name) / "small.sqlite"
        control = PaperControl(
            path, owner_user_id=OWNER, owner_chat_id=CHAT, max_updates=2, max_audit=2
        )
        for i in range(1, 8):
            control.handle_update(update(i, action="u5:pause"), now_ms=NOW + i)
        state = control.snapshot()
        self.assertEqual(
            control.handle_update(update(1, action="u5:enable"), now_ms=NOW + 9), []
        )
        self.assertEqual(control.snapshot(), state)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM paper_control_updates").fetchone()[0],
                2,
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM paper_control_audit").fetchone()[0], 2
            )

    def test_database_loss_fails_closed_existing_instance_new_one_is_disabled(self):
        self.enable()
        self.path.unlink()
        with self.assertRaisesRegex(ControlError, "CONTROL_DATABASE_UNAVAILABLE"):
            self.control.with_permit(lambda _: self.fail("must not run"))
        reopened = PaperControl(self.path, owner_user_id=OWNER, owner_chat_id=CHAT)
        self.assertFalse(reopened.snapshot()["paper_enabled"])

    def test_database_replacement_detected(self):
        self.enable()
        moved = self.path.with_suffix(".saved")
        self.path.rename(moved)
        PaperControl(self.path, owner_user_id=OWNER, owner_chat_id=CHAT)
        with self.assertRaisesRegex(ControlError, "CONTROL_DATABASE_REPLACED"):
            self.control.snapshot()

    def test_foreign_database_and_symlink_untouched(self):
        foreign = Path(self.temp.name) / "historical.sqlite"
        with closing(sqlite3.connect(foreign)) as db:
            db.execute("CREATE TABLE historical(v TEXT)")
        original = foreign.read_bytes()
        with self.assertRaisesRegex(ControlError, "FOREIGN_OR_INCOMPLETE"):
            PaperControl(foreign, owner_user_id=OWNER, owner_chat_id=CHAT)
        self.assertEqual(foreign.read_bytes(), original)
        link = Path(self.temp.name) / "link.sqlite"
        link.symlink_to(foreign)
        with self.assertRaises(ControlError):
            PaperControl(link, owner_user_id=OWNER, owner_chat_id=CHAT)

    def test_configuration_ownership_cannot_be_changed(self):
        state = self.control.snapshot()
        with self.assertRaisesRegex(ControlError, "CONTROL_CONFIG_OR_SCHEMA_CONFLICT"):
            PaperControl(self.path, owner_user_id=OWNER + 1, owner_chat_id=CHAT)
        self.assertEqual(self.control.snapshot(), state)

    def test_clock_regression_cannot_resurrect_confirmation(self):
        action = confirmation(self.send(1, action="u5:enable", at=NOW))
        self.send(2, action="u5:status", at=NOW + 61000)
        with self.assertRaisesRegex(ControlError, "CONTROL_CLOCK_REGRESSION"):
            self.send(3, action=action, at=NOW + 1)
        self.assertFalse(self.control.snapshot()["paper_enabled"])

    def test_malformed_oversized_and_wrong_commands_have_no_effect(self):
        original = self.control.snapshot()
        malformed = [
            None,
            [],
            {},
            {"update_id": True},
            update(1, text="/live"),
            update(1, text="/us500 TOKEN_VALUE_SHOULD_NOT_BE_STORED"),
        ]
        oversized = update(1)
        oversized["unused"] = "x" * 40000
        malformed.append(oversized)
        both = update(1)
        both["callback_query"] = update(1, action="u5:pause")["callback_query"]
        malformed.append(both)
        for item in malformed:
            self.assertEqual(self.control.handle_update(item, now_ms=NOW), [])
        self.assertEqual(self.control.snapshot(), original)
        self.assertNotIn(b"TOKEN_VALUE_SHOULD_NOT_BE_STORED", self.path.read_bytes())

    def test_telegram_input_payload_not_retained_in_sqlite(self):
        item = update(1)
        item["message"]["from"]["first_name"] = "PRIVATE_ACTOR_NAME_NOT_STORED"
        item["message"]["unused"] = "PRIVATE_INPUT_NOT_STORED"
        self.control.handle_update(item, now_ms=NOW)
        raw = self.path.read_bytes()
        self.assertNotIn(b"PRIVATE_ACTOR_NAME_NOT_STORED", raw)
        self.assertNotIn(b"PRIVATE_INPUT_NOT_STORED", raw)

    def test_two_owners_cannot_confirm_in_same_update_number(self):
        item = update(1, action="u5:enable")
        outsider = copy.deepcopy(item)
        outsider["callback_query"]["from"]["id"] = OWNER + 1
        self.assertEqual(self.control.handle_update(outsider, now_ms=NOW), [])
        self.assertTrue(self.control.handle_update(item, now_ms=NOW + 1))

    def test_sqlite_failure_rolls_back_activation_nonce_and_update_id(self):
        action = confirmation(self.send(1, action="u5:enable"))
        original = self.control.snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TRIGGER fail_control_audit BEFORE INSERT ON paper_control_audit BEGIN SELECT RAISE(ABORT, 'INJECTED_STORAGE_FAILURE'); END"
            )
        with self.assertRaises(sqlite3.Error):
            self.send(2, action=action)
        self.assertEqual(self.control.snapshot(), original)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM paper_control_pending").fetchone()[0],
                1,
            )
            db.execute("DROP TRIGGER fail_control_audit")
        self.send(2, action=action)
        self.assertTrue(self.control.snapshot()["paper_enabled"])

    def test_sqlite_failure_does_not_acknowledge_a_pause(self):
        self.enable()
        original = self.control.snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TRIGGER fail_control_audit BEFORE INSERT ON paper_control_audit BEGIN SELECT RAISE(ABORT, 'INJECTED_STORAGE_FAILURE'); END"
            )
        with self.assertRaises(sqlite3.Error):
            self.send(3, action="u5:pause")
        self.assertEqual(self.control.snapshot(), original)

    def test_opaque_callback_identifier_is_preserved_only_for_answer(self):
        item = update(1, action="u5:status")
        item["callback_query"]["id"] = "AQ:12/ab+c=="
        result = self.control.handle_update(item, now_ms=NOW)
        self.assertEqual(
            result[0],
            {
                "method": "answerCallbackQuery",
                "payload": {"callback_query_id": "AQ:12/ab+c=="},
            },
        )

    def test_invalid_nonce_cannot_consume_valid_confirmation(self):
        action = confirmation(self.send(1, action="u5:enable"))
        self.send(2, action="u5:confirm:" + "a" * 24)
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.send(3, action=action)
        self.assertTrue(self.control.snapshot()["paper_enabled"])

    def test_invalid_unicode_payload_is_ignored_without_state_change(self):
        original = self.control.snapshot()
        for action in ("u5:pause", "u5:confirm:\ud800"):
            item = update(1, action=action)
            item["callback_query"]["from"]["first_name"] = "\ud800"
            self.assertEqual(self.control.handle_update(item, now_ms=NOW), [])
            self.assertEqual(self.control.snapshot(), original)

    def test_local_fault_pause_is_durable_and_has_distinct_actor(self):
        self.enable()
        before = self.control.snapshot()
        result = self.control.pause_for_fault("TELEGRAM_UNAVAILABLE", now_ms=NOW + 3)
        self.assertFalse(result["paper_enabled"])
        self.assertEqual(result["reason"], "TELEGRAM_UNAVAILABLE")
        self.assertEqual(result["revision"], before["revision"] + 1)
        self.assertEqual(result["last_update_id"], before["last_update_id"])
        restarted = PaperControl(self.path, owner_user_id=OWNER, owner_chat_id=CHAT)
        self.assertEqual(restarted.snapshot(), result)
        self.assertFalse(result["ledger_risk_pause_cleared"])
        with closing(sqlite3.connect(self.path)) as db:
            audit = db.execute(
                "SELECT action,reason,actor_id,chat_id FROM paper_control_audit ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(audit, ("LOCAL_FAULT_PAUSE", "TELEGRAM_UNAVAILABLE", 0, 0))
        self.assertEqual(self.send(3, action="u5:enable", actor=999), [])
        self.assertFalse(
            self.control.with_permit(lambda _: self.fail("blocked"))["permitted"]
        )

    def test_local_fault_pause_repeated_is_idempotent_without_fake_updates(self):
        first = self.control.pause_for_fault("TELEGRAM_CAPACITY", now_ms=NOW)
        second = self.control.pause_for_fault("TELEGRAM_CAPACITY", now_ms=NOW + 1)
        self.assertEqual(first, second)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM paper_control_updates").fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM paper_control_audit").fetchone()[0], 1
            )

    def test_local_fault_pause_revokes_existing_and_later_confirmations(self):
        first = confirmation(self.send(1, action="u5:enable"))
        paused = self.control.pause_for_fault("TELEGRAM_CAPACITY", now_ms=NOW + 2)
        self.send(2, action=first, at=NOW + 3)
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        second = confirmation(self.send(3, action="u5:enable", at=NOW + 4))
        again = self.control.pause_for_fault("TELEGRAM_CAPACITY", now_ms=NOW + 5)
        self.assertEqual(again["revision"], paused["revision"] + 1)
        self.send(4, action=second, at=NOW + 6)
        self.assertFalse(self.control.snapshot()["paper_enabled"])

    def test_local_fault_pause_only_accepts_whitelist(self):
        original = self.control.snapshot()
        for reason in (None, {}, "anything", "OWNER_PAUSED", "TELEGRAM_TOKEN_value"):
            with (
                self.subTest(reason=type(reason)),
                self.assertRaisesRegex(ControlError, "INVALID_LOCAL_FAULT_REASON"),
            ):
                self.control.pause_for_fault(reason, now_ms=NOW)
            self.assertEqual(self.control.snapshot(), original)

    def test_local_fault_clock_rollback_does_not_block_pause(self):
        self.enable()
        paused = self.control.pause_for_fault("TELEGRAM_UNAVAILABLE", now_ms=NOW - 1000)
        self.assertFalse(paused["paper_enabled"])
        self.assertEqual(paused["updated_at_ms"], NOW + 2)
        with self.assertRaisesRegex(ControlError, "CONTROL_CLOCK_REGRESSION"):
            self.send(3, action="u5:enable", at=NOW + 1)

    def test_local_fault_sqlite_failure_does_not_claim_pause(self):
        self.enable()
        original = self.control.snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TRIGGER fail_local_pause BEFORE INSERT ON paper_control_audit BEGIN SELECT RAISE(ABORT, 'INJECTED_STORAGE_FAILURE'); END"
            )
        with self.assertRaises(sqlite3.Error):
            self.control.pause_for_fault("TELEGRAM_UNAVAILABLE", now_ms=NOW + 3)
        self.assertEqual(self.control.snapshot(), original)

    def test_local_fault_audit_is_bounded(self):
        path = Path(self.temp.name) / "bounded-fault.sqlite"
        control = PaperControl(
            path, owner_user_id=OWNER, owner_chat_id=CHAT, max_audit=2
        )
        for index in range(5):
            reason = "TELEGRAM_CAPACITY" if index % 2 else "TELEGRAM_UNAVAILABLE"
            control.pause_for_fault(reason, now_ms=NOW + index)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM paper_control_audit").fetchone()[0], 2
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM paper_control_updates").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
