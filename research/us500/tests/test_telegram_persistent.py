"""Continuous receiver fault injection: no network, orders, or live services."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_us500_telegram_service import CHAT, OWNER, Clock, FakeAdapter, enable

from us500_control import PaperControl
from us500_telegram_service import ServiceError, TelegramPaperService


class Stop:
    def __init__(self):
        self.waits = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.stopped


class PersistentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.control = PaperControl(
            self.root / "control.sqlite", owner_user_id=OWNER, owner_chat_id=CHAT
        )
        self.adapter = FakeAdapter()
        self.prunes = []
        self.adapter.prune_delivered = lambda **kwargs: self.prunes.append(kwargs)
        self.service = TelegramPaperService(
            self.root / "state",
            control=self.control,
            credential_provider=lambda: "synthetic",
            transport_factory=lambda *a, **kw: SimpleNamespace(bot_id=123),
            adapter_factory=lambda *a, **kw: self.adapter,
            clock_ms=Clock(),
            lock_root=self.root / "locks",
        )
        self.saved = []
        original = self.service._write_status

        def capture(status):
            original(status)
            self.saved.append(json.loads(json.dumps(status)))

        self.service._write_status = capture

    def test_unbounded_only_with_explicit_persistent_mode(self):
        with self.assertRaises(ServiceError):
            self.service.run(None, 3, Stop())
        with self.assertRaises(ServiceError):
            self.service.run(2, 3, Stop(), persistent="true")
        stop = Stop()
        self.adapter.hook = lambda _: setattr(stop, "stopped", self.adapter.calls >= 4)
        result = self.service.run(None, 3, stop, persistent=True)
        self.assertEqual(result["phase"], "STOPPED")
        self.assertEqual(self.adapter.calls, 4)
        self.assertEqual(len(self.prunes), 4)
        self.assertTrue(result["paper_pause_confirmed"])
        self.assertIsNone(result["max_cycles"])

    def test_transient_poll_failures_backoff_pause_and_recover(self):
        enabled_during_recovery = []

        def hook(now):
            if self.adapter.calls == 1:
                enable(self.control, at=now)
            if self.adapter.calls == 3:
                enabled_during_recovery.append(self.control.snapshot()["paper_enabled"])
            self.adapter.result = (
                {"poll_status": "STOPPED", "error": "TELEGRAM_RESPONSE_TIMEOUT"}
                if self.adapter.calls < 3
                else {"poll_status": "COMPLETE"}
            )

        self.adapter.hook = hook
        stop = Stop()
        result = self.service.run(4, 3, stop, persistent=True)
        self.assertEqual(stop.waits, [3, 6, 3])
        self.assertEqual(enabled_during_recovery, [False])
        self.assertEqual(result["phase"], "STOPPED")
        self.assertEqual([s["phase"] for s in self.saved].count("RECOVERING"), 2)

    def test_backoff_is_capped_and_interruptible(self):
        self.adapter.result = {
            "poll_status": "STOPPED",
            "error": "TELEGRAM_RESPONSE_UNKNOWN",
        }
        stop = Stop()
        self.service.run(9, 3, stop, persistent=True)
        self.assertEqual(stop.waits, [3, 6, 12, 24, 48, 60, 60, 60, 60])

    def test_nontransient_rejection_or_invalid_state_does_not_retry(self):
        for code in (
            "TELEGRAM_REQUEST_REJECTED",
            "OUTBOX_BINDING_CONFLICT",
            "INVALID_UPDATES_RESPONSE",
            "GET_UPDATES_FAILED",
        ):
            with self.subTest(code=code):
                self.adapter.calls = 0
                self.adapter.result = {"poll_status": "STOPPED", "error": code}
                stop = Stop()
                result = self.service.run(5, 3, stop, persistent=True)
                self.assertEqual(result["phase"], "FAILED")
                self.assertEqual(self.adapter.calls, 1)
                self.assertEqual(stop.waits, [])

    def test_ambiguous_delivery_keeps_receiver_available_but_paper_paused(self):
        self.adapter.counts = {"UNKNOWN": 1, "SENT": 2}
        result = self.service.run(3, 0, Stop(), persistent=True)
        degraded = [s for s in self.saved if s["phase"] == "DEGRADED"]
        self.assertEqual(len(degraded), 3)
        self.assertTrue(all(s["control"]["paper_enabled"] is False for s in degraded))
        self.assertEqual(result["outbox"]["outbox_counts"]["UNKNOWN"], 1)
        self.assertEqual(self.adapter.calls, 3)

    def test_existing_unknown_cannot_claim_paper_enabled(self):
        from test_us500_telegram_service import callback

        self.adapter.counts = {"UNKNOWN": 1}
        responses = []
        self.adapter.hook = lambda now: responses.extend(
            self.control.handle_update(callback(50, "u5:enable"), now_ms=now)
        )
        result = self.service.run(1, 0, Stop(), persistent=True)
        self.assertFalse(result["control"]["paper_enabled"])
        self.assertIn("sigue pausada", responses[-1]["payload"]["text"])
        self.assertNotIn("u5:confirm:", json.dumps(responses))

    def test_explicit_callback_rejection_is_diagnostic_not_delivery_uncertainty(self):
        self.adapter.counts = {"FAILED": 1, "SENT": 2}
        snapshot = self.adapter.snapshot
        self.adapter.snapshot = lambda: dict(snapshot(), blocking_outbox_counts={})
        self.service.run(2, 0, Stop(), persistent=True)
        self.assertFalse(any(s["phase"] == "DEGRADED" for s in self.saved))
        self.assertTrue(self.control._transport_available)

    def test_invalid_blocking_count_cannot_hide_corrupt_status(self):
        snapshot = self.adapter.snapshot
        self.adapter.snapshot = lambda: dict(
            snapshot(), blocking_outbox_counts={"UNKNOWN": 1}
        )
        result = self.service.run(2, 0, Stop(), persistent=True)
        self.assertEqual(result["phase"], "FAILED")
        self.assertTrue(result["paper_pause_confirmed"])

    def test_pruning_failure_fails_closed(self):
        self.adapter.prune_delivered = lambda **_: (_ for _ in ()).throw(
            ValueError("PRIVATE")
        )
        result = self.service.run(3, 0, threading.Event(), persistent=True)
        self.assertEqual(result["phase"], "FAILED")
        self.assertTrue(result["paper_pause_confirmed"])
        self.assertEqual(self.adapter.calls, 0)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_failure_to_pause_recovery_is_fatal(self):
        self.adapter.result = {
            "poll_status": "STOPPED",
            "error": "TELEGRAM_RESPONSE_TIMEOUT",
        }
        with patch.object(self.service, "_pause", side_effect=[True, False, True]):
            result = self.service.run(3, 0, Stop(), persistent=True)
        self.assertEqual(result["phase"], "FAILED")
        self.assertIn("RECOVERY_PAUSE_UNCONFIRMED", result["errors"])


if __name__ == "__main__":
    unittest.main()
