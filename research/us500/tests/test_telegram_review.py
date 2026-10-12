"""Regresiones TG-01/03/04/05: transportes ficticios, sin servicios reales."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_telegram_persistent as persistent_helpers
import test_us500_telegram as tg_helpers
from test_motor_status_readonly import NOW, Connection, Response, edges, health, status
from test_telegram_persistent import Stop
from test_us500_telegram import CHAT, OWNER, FakeTransport, T, callback, message

from motor_status_readonly import RuntimeStatusReader
from us500_control import PaperControl
from us500_telegram import PaperTelegram, TelegramRateLimited, TelegramRejected


class ReviewTelegramTests(unittest.TestCase):
    def test_oversized_update_is_consumed_without_actions_and_next_command_works(self):
        for owner in (OWNER, 999):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                control = PaperControl(
                    root / "control", owner_user_id=OWNER, owner_chat_id=CHAT
                )
                transport = FakeTransport()
                adapter = PaperTelegram(
                    root / "outbox", control=control, transport=transport
                )
                self.addCleanup(adapter.close)
                transport.updates = [
                    message(1, "PRIVATE" * 6000, owner=owner),
                    message(2),
                ]
                before = control.snapshot()
                with patch.object(
                    control, "handle_update", wraps=control.handle_update
                ) as handle:
                    result = adapter.poll_once(now_ms=T)
                self.assertEqual(result["poll_status"], "COMPLETE")
                self.assertEqual(result["next_offset"], 3)
                self.assertEqual(handle.call_count, 1)
                self.assertEqual(len(transport.sent()), 1)
                self.assertEqual(control.snapshot()["revision"], before["revision"])
                self.assertNotIn(b"PRIVATE", (root / "outbox").read_bytes())
                transport.updates = [message(1, "different" * 6000, owner=owner)]
                self.assertEqual(
                    adapter.poll_once(now_ms=T + 1)["error"], "UPDATE_ID_CONFLICT"
                )

    def test_outgoing_rate_rejection_is_retained_without_retry_and_next_menu_works(
        self,
    ):
        helper = tg_helpers.PaperTelegramTests()
        helper.setUp()
        self.addCleanup(helper.tearDown)
        helper.transport.send_fault = TelegramRateLimited(120)
        result = helper.poll(message(1))
        self.assertEqual(result["error"], "TELEGRAM_RATE_LIMITED")
        self.assertEqual(result["next_offset"], 2)
        self.assertEqual(result["outbox_counts"], {"FAILED": 1})
        self.assertFalse(helper.control.snapshot()["paper_enabled"])
        helper.transport.send_fault = None
        result = helper.poll(message(2), now=T + 120_000)
        self.assertEqual(result["outbox_counts"], {"FAILED": 1, "SENT": 1})
        self.assertEqual(len(helper.transport.sent()), 2)
        helper.poll(message(1), now=T + 120_001)
        self.assertEqual(len(helper.transport.sent()), 2)

    def test_malformed_or_non_health_503_remains_unknown(self):
        for path, payload in (
            ("/health", {}),
            ("/health", {"detail": {"status": "healthy"}}),
            ("/api/status", {"detail": {"status": "unhealthy"}}),
        ):
            reader = RuntimeStatusReader(
                connection_factory=lambda *a, **k: Connection(
                    Response(json.dumps(payload).encode(), 503)
                )
            )
            with (
                self.subTest(path=path, payload=payload),
                self.assertRaises(ValueError),
            ):
                reader._get(path)

    def test_429_is_bounded_explicit_and_does_not_disclose_provider_text(self):
        helper = tg_helpers.TransportTests()
        for delay in (1, 120, 86400):
            transport, _, _ = helper.transport(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": 429,
                        "description": "PRIVATE",
                        "parameters": {"retry_after": delay},
                    }
                ).encode(),
                429,
            )
            with self.assertRaises(TelegramRateLimited) as caught:
                transport.call("getUpdates", helper.get_payload())
            self.assertEqual(caught.exception.retry_after, delay)
            self.assertEqual(str(caught.exception), "TELEGRAM_RATE_LIMITED")
        for delay in (True, -1, 86401, "1", None):
            transport, _, _ = helper.transport(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": 429,
                        "parameters": {"retry_after": delay},
                    }
                ).encode(),
                429,
            )
            with self.assertRaises(TelegramRejected) as caught:
                transport.call("getUpdates", helper.get_payload())
            self.assertNotIsInstance(caught.exception, TelegramRateLimited)

    def test_supervisor_recovers_clock_regression_and_revokes_confirmation(self):
        helper = persistent_helpers.PersistentTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        stop = Stop()

        def hook(now):
            helper.adapter.result = (
                {"poll_status": "STOPPED", "error": "CONTROL_CLOCK_REGRESSION"}
                if helper.adapter.calls == 1
                else {"poll_status": "COMPLETE"}
            )

        helper.adapter.hook = hook
        result = helper.service.run(2, 0, stop, persistent=True)
        self.assertEqual(result["phase"], "STOPPED")
        self.assertEqual(helper.adapter.calls, 2)
        self.assertEqual(stop.waits, [3])
        self.assertFalse(result["control"]["paper_enabled"])

    def test_real_adapter_regression_preserves_cursor_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control = PaperControl(
                root / "control", owner_user_id=OWNER, owner_chat_id=CHAT
            )
            transport = FakeTransport()
            adapter = PaperTelegram(
                root / "outbox", control=control, transport=transport
            )
            self.addCleanup(adapter.close)
            transport.updates = [callback(1, "u5:enable")]
            adapter.poll_once(now_ms=T)
            transport.updates = [message(2)]
            result = adapter.poll_once(now_ms=T - 1000)
            self.assertEqual(result["error"], "CONTROL_CLOCK_REGRESSION")
            self.assertEqual(result["next_offset"], 2)
            self.assertFalse(control.snapshot()["paper_enabled"])
            self.assertEqual(adapter.poll_once(now_ms=T + 1)["next_offset"], 3)

    def test_rate_limit_wait_respects_provider_and_receiver_remains_available(self):
        helper = persistent_helpers.PersistentTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)

        def hook(now):
            helper.adapter.result = (
                {
                    "poll_status": "STOPPED",
                    "error": "TELEGRAM_RATE_LIMITED",
                    "retry_after_seconds": 120,
                }
                if helper.adapter.calls == 1
                else {"poll_status": "COMPLETE"}
            )

        helper.adapter.hook = hook
        stop = Stop()
        result = helper.service.run(2, 0, stop, persistent=True)
        self.assertEqual(sum(stop.waits), 120)
        self.assertLessEqual(max(stop.waits), 60)
        self.assertEqual(result["phase"], "STOPPED")
        self.assertFalse(result["control"]["paper_enabled"])

    def test_503_health_keeps_paused_state_visible_without_claiming_motor_activity(
        self,
    ):
        paused = status()
        paused["bot"]["is_paused"] = True
        connections = iter(
            [
                Connection(
                    Response(
                        json.dumps(
                            {"detail": {**health(), "status": "unhealthy"}}
                        ).encode(),
                        503,
                    )
                ),
                Connection(Response(json.dumps(paused).encode())),
                Connection(Response(json.dumps(edges()).encode())),
            ]
        )
        reader = RuntimeStatusReader(
            connection_factory=lambda *a, **k: next(connections)
        )
        result = reader(now_ms=NOW)
        self.assertTrue(result["fresh"])
        self.assertIs(result["flags"]["bot_paused"], True)
        self.assertIsNone(result["motors"]["m6"]["running"])


if __name__ == "__main__":
    unittest.main()
