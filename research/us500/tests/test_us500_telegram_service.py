"""Supervisor tests use only temporary files, fake transports and bounded threads."""

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from us500_control import PaperControl
from us500_telegram_service import ServiceError, TelegramPaperService, main

T = 1791700000000
OWNER, CHAT = 123, 456
FAKE_SECRET = "12345:" + "test-only-never-log-" * 2


def callback(update_id, data):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "data": data,
            "from": {"id": OWNER, "is_bot": False},
            "message": {
                "date": T // 1000,
                "message_id": 1,
                "chat": {"id": CHAT, "type": "private"},
            },
        },
    }


def enable(control, *, first_id=1, at=T - 10):
    responses = control.handle_update(callback(first_id, "u5:enable"), now_ms=at)
    message = next(
        row["payload"] for row in responses if row["method"] == "sendMessage"
    )
    action = message["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    control.handle_update(callback(first_id + 1, action), now_ms=at + 1)


class Clock:
    def __init__(self):
        self.at = T

    def __call__(self):
        self.at += 1
        return self.at


class FakeAdapter:
    def __init__(self, *, hook=None, result=None, counts=None):
        self.hook = hook
        self.result = result or {"poll_status": "COMPLETE"}
        self.counts = counts or {}
        self.calls = 0
        self.closed = False

    def poll_once(self, *, now_ms):
        self.calls += 1
        if self.hook:
            self.hook(now_ms)
        return self.result

    def snapshot(self):
        return {
            "scope": "US500",
            "mode": "paper",
            "production_control_available": False,
            "next_offset": 0,
            "saved_updates": 0,
            "outbox_counts": dict(self.counts),
        }

    def close(self):
        self.closed = True


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = PaperControl(
            self.root / "control.sqlite", owner_user_id=OWNER, owner_chat_id=CHAT
        )
        self.adapter = FakeAdapter()
        self.clock = Clock()
        self.credentials_read = 0
        self.transports = []
        self.created_adapters = []

    def tearDown(self):
        self.temp.cleanup()

    def credentials(self):
        self.credentials_read += 1
        return FAKE_SECRET

    def transport(self, token, *, owner_chat_id):
        self.transports.append(
            (token, owner_chat_id, self.control.snapshot()["paper_enabled"])
        )
        return SimpleNamespace(bot_id=12345)

    def make_service(self, *, state_dir=None, control=None, adapter=None, **changes):
        selected = adapter or self.adapter

        def factory(path, *, control, transport):
            self.created_adapters.append(path)
            return selected

        args = dict(
            control=control or self.control,
            credential_provider=self.credentials,
            transport_factory=self.transport,
            adapter_factory=factory,
            clock_ms=self.clock,
            lock_root=self.root / "locks",
        )
        args.update(changes)
        return TelegramPaperService(state_dir or self.root / "state", **args)

    def assert_redacted(self, status, service):
        self.assertNotIn(FAKE_SECRET, json.dumps(status))
        self.assertNotIn("US500_PAPER_TELEGRAM_TOKEN", json.dumps(status))
        if service.status_path.exists():
            self.assertNotIn(FAKE_SECRET, service.status_path.read_text())

    def test_bounded_run_pauses_before_credentials_and_after_shutdown(self):
        enable(self.control)
        self.assertTrue(self.control.snapshot()["paper_enabled"])
        service = self.make_service()
        status = service.run(3, 0, threading.Event())
        self.assertEqual(status["phase"], "STOPPED")
        self.assertEqual(status["completed_cycles"], 3)
        self.assertEqual(self.adapter.calls, 3)
        self.assertEqual(self.credentials_read, 1)
        self.assertEqual(self.transports[0][2], False)
        self.assertTrue(status["paper_pause_confirmed"])
        self.assertEqual(status["pause_cause"], "SERVICE_SHUTDOWN")
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.assertFalse(status["paper_runner_started"])
        self.assertFalse(status["kalshi_access"])
        self.assertTrue(self.adapter.closed)
        self.assertTrue(status["status_persisted"])
        self.assertEqual(json.loads(service.status_path.read_text()), status)
        self.assertEqual(service.status_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(service.state_dir.glob(".us500-telegram-status-*")), [])
        self.assert_redacted(status, service)

    def test_stop_before_run_never_reads_credentials(self):
        enable(self.control)
        stop = threading.Event()
        stop.set()
        service = self.make_service()
        status = service.run(5, 100, stop)
        self.assertEqual(status["completed_cycles"], 0)
        self.assertEqual(self.credentials_read, 0)
        self.assertEqual(self.created_adapters, [])
        self.assertEqual(status["phase"], "STOPPED")
        self.assertTrue(status["paper_pause_confirmed"])

    def test_stop_event_interrupts_interval_and_shutdown_revokes_paper_enablement(self):
        stop = threading.Event()

        def hook(_):
            enable(self.control, at=T + 100)
            stop.set()

        self.adapter.hook = hook
        service = self.make_service()
        status = service.run(10, 3600, stop)
        self.assertEqual(status["completed_cycles"], 1)
        self.assertEqual(status["phase"], "STOPPED")
        self.assertFalse(self.control.snapshot()["paper_enabled"])

    def test_failed_cycle_pauses_and_sanitizes_raw_exception(self):
        def hook(_):
            enable(self.control, at=T + 100)
            raise RuntimeError("https://example.test/" + FAKE_SECRET)

        self.adapter.hook = hook
        service = self.make_service()
        status = service.run(10, 0, threading.Event())
        self.assertEqual(status["phase"], "FAILED")
        self.assertIn("POLL_CYCLE_FAILED", status["errors"])
        self.assertEqual(self.adapter.calls, 1)
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.assertTrue(status["paper_pause_confirmed"])
        self.assert_redacted(status, service)

    def test_stopped_cycle_and_unknown_outbox_both_fail_closed(self):
        for reason in ("STOPPED", "UNKNOWN"):
            with self.subTest(reason=reason):
                adapter = (
                    FakeAdapter(result={"poll_status": "STOPPED"})
                    if reason == "STOPPED"
                    else FakeAdapter(counts={"UNKNOWN": 1})
                )
                service = self.make_service(
                    state_dir=self.root / reason, adapter=adapter
                )
                status = service.run(10, 0, threading.Event())
                self.assertEqual(status["phase"], "FAILED")
                self.assertTrue(status["paper_pause_confirmed"])
                self.assertEqual(adapter.calls, 1)
                self.assertTrue(adapter.closed)

    def test_credential_failure_is_redacted_and_keeps_control_disabled(self):
        def credentials():
            raise RuntimeError(FAKE_SECRET)

        service = self.make_service(credential_provider=credentials)
        status = service.run(1, 0, threading.Event())
        self.assertIn("CREDENTIAL_OR_TRANSPORT_UNAVAILABLE", status["errors"])
        self.assertFalse(self.control.snapshot()["paper_enabled"])
        self.assertTrue(status["paper_pause_confirmed"])
        self.assertEqual(self.created_adapters, [])
        self.assert_redacted(status, service)

    def test_pause_failure_never_claims_confirmed_or_polls(self):
        enable(self.control)
        service = self.make_service()
        with patch.object(
            self.control, "pause_for_fault", side_effect=RuntimeError(FAKE_SECRET)
        ):
            status = service.run(1, 0, threading.Event())
        self.assertFalse(status["paper_pause_confirmed"])
        self.assertIn("STARTUP_PAUSE_UNCONFIRMED", status["errors"])
        self.assertIn("SHUTDOWN_PAUSE_UNCONFIRMED", status["errors"])
        self.assertEqual(self.credentials_read, 0)
        self.assertTrue(self.control.snapshot()["paper_enabled"])
        self.assert_redacted(status, service)

    def test_shutdown_pause_failure_is_explicit(self):
        original = self.control.pause_for_fault
        calls = 0

        def pause(reason, *, now_ms):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError(FAKE_SECRET)
            return original(reason, now_ms=now_ms)

        self.adapter.hook = lambda _: enable(self.control, at=T + 100)
        service = self.make_service()
        with patch.object(self.control, "pause_for_fault", pause):
            status = service.run(1, 0, threading.Event())
        self.assertEqual(status["phase"], "FAILED")
        self.assertFalse(status["paper_pause_confirmed"])
        self.assertIn("SHUTDOWN_PAUSE_UNCONFIRMED", status["errors"])
        self.assertTrue(self.control.snapshot()["paper_enabled"])
        self.assert_redacted(status, service)

    def test_atomic_status_failure_does_not_claim_a_saved_result(self):
        service = self.make_service()
        with patch.object(service, "_write_status", side_effect=OSError(FAKE_SECRET)):
            status = service.run(1, 0, threading.Event())
        self.assertEqual(status["phase"], "FAILED")
        self.assertFalse(status["status_persisted"])
        self.assertIn("STATUS_WRITE_FAILED", status["errors"])
        self.assertTrue(status["paper_pause_confirmed"])
        self.assertEqual(self.credentials_read, 0)
        self.assert_redacted(status, service)

    def test_foreign_status_and_symlink_state_are_not_overwritten(self):
        service = self.make_service()
        service.state_dir.mkdir()
        original = b"foreign-file-do-not-replace"
        service.status_path.write_bytes(original)
        status = service.run(1, 0, threading.Event())
        self.assertEqual(service.status_path.read_bytes(), original)
        self.assertFalse(status["status_persisted"])
        alias = self.root / "alias"
        alias.symlink_to(service.state_dir, target_is_directory=True)
        other = self.make_service(state_dir=alias)
        rejected = other.run(1, 0, threading.Event())
        self.assertIn("UNSAFE_SERVICE_DIRECTORY", rejected["errors"])
        self.assertEqual(service.status_path.read_bytes(), original)

    def test_invalid_limits_have_no_service_side_effects(self):
        service = self.make_service()
        for maximum, interval in (
            (0, 1),
            (10001, 1),
            (True, 1),
            (1, -1),
            (1, float("nan")),
            (1, True),
        ):
            with (
                self.subTest(maximum=maximum, interval=interval),
                self.assertRaises(ServiceError),
            ):
                service.run(maximum, interval, threading.Event())
        self.assertFalse(service.state_dir.exists())
        self.assertEqual(self.credentials_read, 0)

    def test_keyboard_interrupt_still_pauses_and_closes(self):
        def interrupt(_):
            raise KeyboardInterrupt

        self.adapter.hook = interrupt
        service = self.make_service()
        status = service.run(10, 0, threading.Event())
        self.assertEqual(status["phase"], "STOPPED")
        self.assertIn("INTERRUPTED", status["errors"])
        self.assertTrue(status["paper_pause_confirmed"])
        self.assertTrue(self.adapter.closed)

    def test_concurrent_receiver_is_blocked_by_directory_control_and_bot(self):
        started, release = threading.Event(), threading.Event()

        def hold(_):
            started.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization timeout")

        first_adapter = FakeAdapter(hook=hold)
        first = self.make_service(adapter=first_adapter)
        completed = []
        thread = threading.Thread(
            target=lambda: completed.append(first.run(1, 0, threading.Event()))
        )
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            before = first.status_path.read_bytes()
            revision = self.control.snapshot()["revision"]
            same_dir = self.make_service(adapter=FakeAdapter())
            status = same_dir.run(1, 0, threading.Event())
            self.assertIn("SERVICE_ALREADY_RUNNING", status["errors"])
            self.assertFalse(status["status_persisted"])
            self.assertEqual(first.status_path.read_bytes(), before)
            self.assertEqual(self.control.snapshot()["revision"], revision)
            same_control = self.make_service(
                state_dir=self.root / "other-dir", adapter=FakeAdapter()
            )
            status = same_control.run(1, 0, threading.Event())
            self.assertIn("SERVICE_ALREADY_RUNNING", status["errors"])
            self.assertEqual(self.control.snapshot()["revision"], revision)
            second_control = PaperControl(
                self.root / "other-control.sqlite",
                owner_user_id=OWNER,
                owner_chat_id=CHAT,
            )
            second_adapter = FakeAdapter()
            same_bot = self.make_service(
                state_dir=self.root / "other-bot-dir",
                control=second_control,
                adapter=second_adapter,
            )
            status = same_bot.run(1, 0, threading.Event())
            self.assertIn("SERVICE_ALREADY_RUNNING", status["errors"])
            self.assertEqual(second_adapter.calls, 0)
            self.assertTrue(status["paper_pause_confirmed"])
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(completed[0]["phase"], "STOPPED")
        # Locks are released on shutdown; their files intentionally remain so a
        # competing process cannot lock a newly created inode at the same name.
        after = self.make_service(adapter=FakeAdapter())
        self.assertEqual(after.run(1, 0, threading.Event())["phase"], "STOPPED")

    def test_cli_without_token_never_contacts_network(self):
        root = self.root / "cli"
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(output):
            result = main(
                [
                    "--state-dir",
                    str(root),
                    "--owner-user-id",
                    str(OWNER),
                    "--owner-chat-id",
                    str(CHAT),
                    "--max-cycles",
                    "1",
                ]
            )
        state = json.loads(output.getvalue())
        self.assertEqual(result, 2)
        self.assertEqual(state["completed_cycles"], 0)
        self.assertTrue(state["paper_pause_confirmed"])
        self.assertIn("CREDENTIAL_OR_TRANSPORT_UNAVAILABLE", state["errors"])
        self.assertFalse((root / "outbox.sqlite").exists())


if __name__ == "__main__":
    unittest.main()
