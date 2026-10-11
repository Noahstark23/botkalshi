"""Telegram control/outbox tests use fakes; never read real tokens or use network."""

import contextlib
import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from us500_control import PaperControl
from us500_telegram import (
    HOST,
    MAX_BYTES,
    PaperTelegram,
    TelegramError,
    TelegramRejected,
    TelegramTransport,
    main,
)

T = 1791700000000
OWNER = 123
CHAT = 456
FAKE_TOKEN = "12345:" + "x" * 30


def message(update_id, text="/us500", *, owner=OWNER):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 1,
            "date": T // 1000,
            "text": text,
            "from": {"id": owner, "is_bot": False},
            "chat": {"id": CHAT, "type": "private"},
        },
    }


def callback(update_id, data):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"query-{update_id}",
            "data": data,
            "from": {"id": OWNER, "is_bot": False},
            "message": {
                "message_id": 1,
                "date": T // 1000,
                "chat": {"id": CHAT, "type": "private"},
            },
        },
    }


class FakeTransport:
    bot_id = 12345

    def __init__(self):
        self.updates = []
        self.calls = []
        self.send_fault = None
        self.get_fault = None
        self.before_send = None

    def call(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == "getUpdates":
            if self.get_fault:
                raise self.get_fault
            return {"ok": True, "result": copy.deepcopy(self.updates)}
        if self.before_send:
            self.before_send(method, payload)
        if self.send_fault:
            if isinstance(self.send_fault, BaseException):
                raise self.send_fault
            return self.send_fault
        if method == "answerCallbackQuery":
            return {"ok": True, "result": True}
        return {
            "ok": True,
            "result": {"message_id": len(self.calls), "chat": {"id": CHAT}},
        }

    def sent(self):
        return [call for call in self.calls if call[0] != "getUpdates"]


class PaperTelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = PaperControl(
            self.root / "control.sqlite", owner_user_id=OWNER, owner_chat_id=CHAT
        )
        self.transport = FakeTransport()
        self.path = self.root / "outbox.sqlite"
        self.adapter = PaperTelegram(
            self.path, control=self.control, transport=self.transport
        )

    def tearDown(self):
        self.adapter.close()
        self.temp.cleanup()

    def poll(self, *updates, now=T):
        self.transport.updates = list(updates)
        return self.adapter.poll_once(now_ms=now)

    def enable(self):
        self.poll(callback(1, "u5:enable"))
        confirmations = [
            payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            for method, payload in self.transport.sent()
            if method == "sendMessage" and "reply_markup" in payload
        ]
        self.assertEqual(len(confirmations), 1)
        update = callback(2, confirmations[0])
        self.poll(update, now=T + 1)
        return update

    def test_menu_is_persisted_before_cursor_and_before_http_attempt(self):
        observed = []

        def check(method, payload):
            with contextlib.closing(sqlite3.connect(self.path)) as db:
                observed.append(
                    (
                        db.execute("SELECT next_offset FROM telegram_meta").fetchone()[
                            0
                        ],
                        db.execute("SELECT status FROM telegram_outbox").fetchone()[0],
                    )
                )

        self.transport.before_send = check
        state = self.poll(message(5))
        self.assertEqual(observed, [(6, "ATTEMPTED")])
        self.assertEqual(state["outbox_counts"], {"SENT": 1})
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.assertEqual(state["next_offset"], 6)

    def test_confirmation_enable_pause_and_duplicate_cannot_reactivate(self):
        confirm = self.enable()
        self.assertTrue(self.control.snapshot()["paper_enabled"])
        self.poll(callback(3, "u5:pause"), now=T + 2)
        paused = self.control.snapshot()
        self.assertFalse(paused["paper_enabled"])
        sent = len(self.transport.sent())
        self.poll(confirm, now=T + 3)
        self.assertEqual(self.control.snapshot(), paused)
        self.assertEqual(len(self.transport.sent()), sent)

    def test_unknown_send_is_not_retried_after_restart_or_duplicate(self):
        self.transport.send_fault = RuntimeError(
            "https://api.telegram.org/bot" + FAKE_TOKEN
        )
        update = message(1)
        state = self.poll(update)
        self.assertEqual(state["outbox_counts"], {"UNKNOWN": 1})
        self.assertNotIn(FAKE_TOKEN, json.dumps(state))
        self.adapter.close()
        self.adapter = PaperTelegram(
            self.path, control=self.control, transport=self.transport
        )
        self.transport.send_fault = None
        self.poll(update, now=T + 1)
        self.assertEqual(len(self.transport.sent()), 1)
        self.assertNotIn(FAKE_TOKEN.encode(), self.path.read_bytes())

    def test_interrupted_attempt_recovers_unknown_without_retry(self):
        self.transport.send_fault = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.poll(message(1))
        self.assertEqual(self.adapter.snapshot()["outbox_counts"], {"ATTEMPTED": 1})
        self.adapter.close()
        self.adapter = PaperTelegram(
            self.path, control=self.control, transport=self.transport
        )
        self.assertEqual(self.adapter.snapshot()["outbox_counts"], {"UNKNOWN": 1})
        self.transport.send_fault = None
        self.poll(message(1), now=T + 1)
        self.assertEqual(len(self.transport.sent()), 1)

    def test_control_commit_before_outbox_failure_recovers_without_second_mutation(
        self,
    ):
        update = callback(1, "u5:pause")
        original = self.control.handle_update

        def fail_after_control(update, *, now_ms):
            original(update, now_ms=now_ms)
            raise RuntimeError("simulated crash after committed control")

        with patch.object(self.control, "handle_update", fail_after_control):
            stopped = self.poll(update)
        self.assertEqual(stopped["poll_status"], "STOPPED")
        self.assertEqual(stopped["next_offset"], 0)
        self.assertEqual(stopped["outbox_counts"], {})
        revision = self.control.snapshot()["revision"]
        self.poll(update, now=T + 1)
        self.assertEqual(self.control.snapshot()["revision"], revision)
        self.assertEqual(self.adapter.snapshot()["next_offset"], 2)
        self.assertEqual(len(self.transport.sent()), 2)

    def test_batch_unsorted_updates_preserve_pause_order(self):
        self.enable()
        self.poll(callback(4, "u5:status"), callback(3, "u5:pause"), now=T + 2)
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.assertEqual(self.adapter.snapshot()["next_offset"], 5)

    def test_conflicting_update_id_does_not_move_cursor_or_send(self):
        self.poll(message(5))
        before = self.adapter.snapshot()
        state = self.poll(message(5, "/motores"), now=T + 1)
        self.assertEqual(state["error"], "UPDATE_ID_CONFLICT")
        self.assertEqual(state["next_offset"], before["next_offset"])
        self.assertEqual(state["outbox_counts"], before["outbox_counts"])
        self.assertEqual(len(self.transport.sent()), 1)

    def test_conflict_inside_batch_is_rejected_before_external_control_actions(self):
        result = self.poll(callback(1, "u5:pause"), callback(1, "u5:enable"))
        self.assertEqual(result["error"], "UPDATE_ID_CONFLICT")
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.assertTrue(result["paper_pause_confirmed"])
        self.assertEqual(result["next_offset"], 0)

    def test_unauthorized_sender_is_consumed_without_control_or_response(self):
        before = self.control.snapshot()
        result = self.poll(message(1, owner=999))
        self.assertEqual(result["next_offset"], 2)
        self.assertEqual(result["outbox_counts"], {})
        self.assertEqual(self.control.snapshot(), before)

    def test_failed_rejection_and_malformed_ack_are_never_retried(self):
        self.transport.send_fault = TelegramRejected("anything unsafe " + FAKE_TOKEN)
        first = self.poll(message(1))
        self.assertEqual(first["outbox_counts"], {"FAILED": 1})
        self.transport.send_fault = {
            "ok": True,
            "result": {"message_id": 42, "chat": {"id": 999}},
        }
        second = self.poll(message(2), now=T + 1)
        self.assertEqual(second["outbox_counts"], {"FAILED": 1, "UNKNOWN": 1})
        self.transport.send_fault = None
        self.poll(message(1), message(2), now=T + 2)
        self.assertEqual(len(self.transport.sent()), 2)

    def test_provider_errors_are_sanitized_and_leave_cursor_unchanged(self):
        self.transport.get_fault = TelegramError(FAKE_TOKEN)
        result = self.poll(message(1))
        self.assertEqual(result["poll_status"], "STOPPED")
        self.assertEqual(result["error"], "CONTROL_OR_TRANSPORT_FAILED")
        self.assertEqual(result["next_offset"], 0)
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))

    def test_limits_preserve_pending_history_and_allow_duplicate(self):
        with patch("us500_telegram.MAX_UPDATES", 1):
            self.poll(message(1))
            result = self.poll(message(2), now=T + 1)
            self.assertEqual(result["error"], "UPDATE_CAPACITY_EXHAUSTED")
            self.assertEqual(result["next_offset"], 2)
            result = self.poll(message(1), now=T + 2)
            self.assertEqual(result["poll_status"], "COMPLETE")
        with patch("us500_telegram.MAX_OUTBOX", 2):
            result = self.poll(message(2), now=T + 3)
            self.assertEqual(result["error"], "OUTBOX_CAPACITY_EXHAUSTED")
            self.assertEqual(result["next_offset"], 2)

    def test_existing_database_and_wrong_bot_are_rejected(self):
        before = (self.root / "control.sqlite").read_bytes()
        with self.assertRaisesRegex(TelegramError, "FOREIGN_OR_INCOMPLETE"):
            PaperTelegram(
                self.root / "control.sqlite",
                control=self.control,
                transport=self.transport,
            )
        self.assertEqual((self.root / "control.sqlite").read_bytes(), before)
        other = FakeTransport()
        other.bot_id = 999
        with self.assertRaisesRegex(TelegramError, "OUTBOX_BINDING_CONFLICT"):
            PaperTelegram(self.path, control=self.control, transport=other)

    def test_batch_bound_and_duplicate_delivery_bound(self):
        state = self.poll(*(message(i) for i in range(11)))
        self.assertEqual(state["error"], "INVALID_UPDATES_RESPONSE")
        self.assertEqual(state["saved_updates"], 0)
        self.assertEqual(self.transport.sent(), [])

    def test_printable_ascii_callback_id_commits_response_and_cursor(self):
        update = callback(1, "u5:pause")
        update["callback_query"]["id"] = "AQ:12/ab+c=="
        state = self.poll(update)
        self.assertEqual(state["poll_status"], "COMPLETE")
        self.assertEqual(state["next_offset"], 2)
        self.assertEqual(state["outbox_counts"], {"SENT": 2})
        self.assertEqual(
            self.transport.sent()[0][1]["callback_query_id"], "AQ:12/ab+c=="
        )

    def test_capacity_failures_pause_enabled_control_without_deleting_history(self):
        for constant, limit in (("MAX_UPDATES", 2), ("MAX_OUTBOX", 5)):
            with self.subTest(constant=constant):
                if self.adapter.snapshot()["saved_updates"]:
                    self.adapter.close()
                    self.control = PaperControl(
                        self.root / f"control-{constant}.sqlite",
                        owner_user_id=OWNER,
                        owner_chat_id=CHAT,
                    )
                    self.transport = FakeTransport()
                    self.path = self.root / f"outbox-{constant}.sqlite"
                    self.adapter = PaperTelegram(
                        self.path, control=self.control, transport=self.transport
                    )
                self.enable()
                before = self.adapter.snapshot()
                with patch("us500_telegram." + constant, limit):
                    result = self.poll(callback(3, "u5:pause"), now=T + 2)
                self.assertEqual(result["poll_status"], "STOPPED")
                self.assertTrue(result["paper_pause_confirmed"])
                self.assertIsNone(result["pause_error"])
                self.assertFalse(self.control.snapshot()["paper_enabled"])
                self.assertEqual(self.control.snapshot()["reason"], "TELEGRAM_CAPACITY")
                self.assertEqual(result["next_offset"], before["next_offset"])
                self.assertEqual(result["saved_updates"], before["saved_updates"])
                self.assertEqual(result["outbox_counts"], before["outbox_counts"])

    def test_get_failure_pauses_locally_and_never_logs_secret(self):
        self.enable()
        self.transport.get_fault = RuntimeError(FAKE_TOKEN)
        result = self.poll(now=T + 2)
        self.assertEqual(result["error"], "GET_UPDATES_FAILED")
        self.assertTrue(result["paper_pause_confirmed"])
        self.assertEqual(self.control.snapshot()["reason"], "TELEGRAM_UNAVAILABLE")
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))

    def test_failed_durable_pause_reports_unconfirmed_without_claiming_success(self):
        self.enable()
        self.transport.get_fault = RuntimeError(FAKE_TOKEN)
        with patch.object(
            self.control,
            "pause_for_fault",
            side_effect=sqlite3.OperationalError(FAKE_TOKEN),
        ):
            result = self.poll(now=T + 2)
        self.assertFalse(result["paper_pause_confirmed"])
        self.assertEqual(result["pause_error"], "PAUSE_UNCONFIRMED")
        self.assertTrue(self.control.snapshot()["paper_enabled"])
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))


class Response:
    def __init__(self, raw, status=200):
        self.raw = raw
        self.status = status

    def read1(self, count):
        chunk, self.raw = self.raw[:count], self.raw[count:]
        return chunk


class Connection:
    sock = None

    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def transport(self, raw=b'{"ok":true,"result":[]}', status=200):
        connection = Connection(Response(raw, status))
        hosts = []

        def factory(host, *, timeout):
            hosts.append((host, timeout))
            return connection

        return (
            TelegramTransport(
                FAKE_TOKEN, owner_chat_id=CHAT, connection_factory=factory
            ),
            connection,
            hosts,
        )

    def get_payload(self):
        return {
            "offset": 0,
            "limit": 10,
            "timeout": 0,
            "allowed_updates": ["message", "callback_query"],
        }

    def test_fixed_host_method_and_no_proxy_or_redirect(self):
        transport, connection, hosts = self.transport()
        self.assertTrue(transport.call("getUpdates", self.get_payload())["ok"])
        self.assertEqual(hosts, [(HOST, 5)])
        args, kwargs = connection.requests[0]
        self.assertEqual(args, ("POST", "/bot" + FAKE_TOKEN + "/getUpdates"))
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertTrue(connection.closed)
        transport, connection, hosts = self.transport(status=302)
        with self.assertRaisesRegex(TelegramError, "RESPONSE_UNKNOWN"):
            transport.call("getUpdates", self.get_payload())
        self.assertEqual(len(hosts), 1)
        self.assertEqual(len(connection.requests), 1)

    def test_methods_recipients_and_token_injection_rejected_before_connection(self):
        transport, connection, hosts = self.transport()
        for method, payload in (
            ("sendPhoto", {}),
            ("sendMessage", {"chat_id": 999, "text": "blocked"}),
            ("getUpdates", {**self.get_payload(), "timeout": True}),
        ):
            with self.subTest(method=method), self.assertRaises(TelegramError):
                transport.call(method, payload)
        self.assertEqual(hosts, [])
        for token in (FAKE_TOKEN + "/getMe", "https://example.com/" + FAKE_TOKEN, None):
            with self.subTest(token=type(token)), self.assertRaises(TelegramError):
                TelegramTransport(token, owner_chat_id=CHAT)

    def test_bounded_invalid_and_rejected_responses(self):
        for raw in (
            b'{"ok":true,"ok":false}',
            b'{"ok":NaN}',
            b"bad",
            b"x" * (MAX_BYTES + 1),
        ):
            transport, connection, _ = self.transport(raw=raw)
            with self.subTest(size=len(raw)), self.assertRaises(TelegramError):
                transport.call("getUpdates", self.get_payload())
            self.assertTrue(connection.closed)
        transport, _, _ = self.transport(raw=b'{"ok":false,"description":"sensitive"}')
        with self.assertRaisesRegex(TelegramRejected, "REQUEST_REJECTED"):
            transport.call("getUpdates", self.get_payload())

    def test_exception_messages_never_expose_token(self):
        def fail(host, *, timeout):
            raise OSError("https://api.telegram.org/bot" + FAKE_TOKEN + "/sendMessage")

        transport = TelegramTransport(
            FAKE_TOKEN, owner_chat_id=CHAT, connection_factory=fail
        )
        with self.assertRaises(TelegramError) as error:
            transport.call("getUpdates", self.get_payload())
        self.assertEqual(str(error.exception), "TELEGRAM_RESPONSE_UNKNOWN")
        self.assertNotIn(FAKE_TOKEN, repr(error.exception))

    def test_cli_missing_token_creates_no_files_or_network(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.dict(os.environ, {}, clear=True),
        ):
            control, outbox = (
                Path(root) / "control.sqlite",
                Path(root) / "outbox.sqlite",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(
                    [
                        "--control-db",
                        str(control),
                        "--outbox-db",
                        str(outbox),
                        "--owner-user-id",
                        str(OWNER),
                        "--owner-chat-id",
                        str(CHAT),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertEqual(
                json.loads(output.getvalue())["error"], "TELEGRAM_TOKEN_REQUIRED"
            )
            self.assertFalse(control.exists())
            self.assertFalse(outbox.exists())


if __name__ == "__main__":
    unittest.main()
