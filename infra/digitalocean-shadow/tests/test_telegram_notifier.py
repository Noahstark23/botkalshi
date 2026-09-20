from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import Message
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location("telegram_notifier", BASE / "telegram_notifier.py")
telegram = importlib.util.module_from_spec(spec)
spec.loader.exec_module(telegram)


class FakeResponse:
    def __init__(self, body: dict, *, status: int = 200):
        self.raw = json.dumps(body).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, limit: int) -> bytes:
        return self.raw[:limit]


class TelegramNotifierTests(unittest.TestCase):
    NOW = datetime(2026, 9, 20, 20, 0, tzinfo=UTC)
    TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghi"
    CHAT_ID = "-1001234567890"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / "packets").mkdir()
        when = (self.NOW - timedelta(seconds=10)).isoformat()
        self.write(
            "health.json",
            {
                "running": True,
                "updated_at": when,
                "mode": "SHADOW_READONLY",
                "last_cycle_id": "cycle-telegram-1",
                "kalshi_markets": 1,
                "execution_authorized": False,
            },
        )
        self.write(
            "packets/latest.json",
            {
                "schema_version": "botkalshi-research-packet-v1",
                "packet_id": "cycle-telegram-1",
                "generated_at": when,
                "mode": "SHADOW_READONLY",
                "execution_authorized": False,
                "kalshi": {"market_count": 1, "markets": [{"ticker": "DO-NOT-RELAY"}]},
            },
        )
        self.write(
            "coverage.json",
            {
                "schema_version": "botkalshi-coverage-v2",
                "cycle_id": "cycle-telegram-1",
                "generated_at": when,
                "series": "KXMLBGAME",
                "open_markets_seen": 1,
                "eligible_in_horizon": 1,
                "orderbooks_fetched": 1,
                "cursor_exhausted": True,
                "truncated_by_page_limit": False,
                "truncated_by_orderbook_limit": False,
            },
        )
        self.write(
            "risk-status.json",
            {
                "schema_version": "botkalshi-risk-status-v2",
                "cycle_id": "cycle-telegram-1",
                "generated_at": when,
                "bank_state": "PENDING_RECONCILIATION",
                "execution_authorized": False,
                "order_capability_present": False,
                "real_entry_eligible": False,
            },
        )
        self.write(
            "assistant/assessment-latest.json",
            {
                "schema_version": "botkalshi-assistant-assessment-v1",
                "assessment_id": "assessment-1",
                "created_at": when,
                "provider": "offline",
                "decision": "NO_ACTION",
                "source_cycle_id": "cycle-telegram-1",
                "summary": "MALICIOUS FREE TEXT MUST NOT BE RELAYED",
                "execution_authorized": False,
                "order_capability_present": False,
            },
        )
        self.env = {
            "TELEGRAM_BOT_TOKEN": self.TOKEN,
            "TELEGRAM_CHAT_ID": self.CHAT_ID,
            "TRADING_ENABLED": "false",
        }

    def write(self, relative: str, value: dict) -> None:
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def delivered_response(self, message_id: int = 41) -> FakeResponse:
        return FakeResponse(
            {
                "ok": True,
                "result": {"message_id": message_id, "chat": {"id": int(self.CHAT_ID)}},
            }
        )

    def set_control(self, paused: bool, *, root: Path | None = None) -> None:
        base = root or self.data
        path = base / "assistant/control-state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "botkalshi-assistant-control-v1",
                    "paused": paused,
                    "reason": "test",
                    "updated_at": self.NOW.isoformat(),
                    "updated_by": "operator",
                }
            ),
            encoding="utf-8",
        )

    def set_assessment(
        self,
        *,
        provider: str = "openai",
        decision: str = "NO_ACTION",
        assessment_id: str = "assessment-1",
        source_cycle_id: str = "cycle-telegram-1",
        created_at: datetime | None = None,
        root: Path | None = None,
    ) -> None:
        base = root or self.data
        path = base / "assistant/assessment-latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "botkalshi-assistant-assessment-v1",
                    "assessment_id": assessment_id,
                    "created_at": (created_at or self.NOW).isoformat(),
                    "provider": provider,
                    "decision": decision,
                    "source_cycle_id": source_cycle_id,
                    "summary": "MALICIOUS FREE TEXT MUST NOT BE RELAYED",
                    "execution_authorized": False,
                    "order_capability_present": False,
                }
            ),
            encoding="utf-8",
        )

    def test_first_verified_cycle_sends_recovery_then_deduplicates(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            first = telegram.notify(self.data, now=self.NOW)
            second = telegram.notify(self.data, now=self.NOW)

        self.assertTrue(first["delivered"])
        self.assertEqual(first["event"], "recovery")
        self.assertFalse(second["delivered"])
        self.assertEqual(second["reason"], "UNCHANGED")
        self.assertEqual(remote.call_count, 1)
        request = remote.call_args.args[0]
        body = json.loads(request.data)
        self.assertIn("Botkalshi volvió", body["text"])
        self.assertIn("Trading real: BLOQUEADO", body["text"])
        self.assertNotIn("parse_mode", body)
        self.assertNotIn("MALICIOUS FREE TEXT", body["text"])
        self.assertNotIn("DO-NOT-RELAY", body["text"])
        self.assertLessEqual(len(body["text"]), 4096)
        state = json.loads((self.data / "assistant/telegram/telegram-state.json").read_text())
        self.assertEqual(
            set(state),
            {
                "schema_version",
                "updated_at",
                "last_delivery_key",
                "last_event",
                "last_technical_status",
                "last_blockers_digest",
                "last_ai_health",
                "last_control_paused",
                "last_assessment_id",
                "last_cycle_id",
                "last_message_id",
                "execution_authorized",
                "order_capability_present",
            },
        )
        self.assertEqual(state["schema_version"], "botkalshi-telegram-state-v2")
        self.assertFalse(state["execution_authorized"])
        self.assertFalse(state["order_capability_present"])

    def test_recovery_dedup_does_not_change_with_assessment(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            first = telegram.notify(self.data, event="recovery", now=self.NOW)
            assessment = json.loads((self.data / "assistant/assessment-latest.json").read_text())
            assessment["assessment_id"] = "assessment-2"
            assessment["decision"] = "REQUEST_HUMAN_REVIEW"
            self.write("assistant/assessment-latest.json", assessment)
            second = telegram.notify(self.data, event="recovery", now=self.NOW)
        self.assertTrue(first["delivered"])
        self.assertFalse(second["delivered"])
        self.assertEqual(second["reason"], "DUPLICATE")
        self.assertEqual(remote.call_count, 1)

    def test_explicit_recovery_requires_verified_cycle_and_does_not_send(self):
        health = json.loads((self.data / "health.json").read_text())
        health["running"] = False
        self.write("health.json", health)
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open"
        ) as remote:
            with self.assertRaisesRegex(telegram.TelegramNotificationError, "verified cycle"):
                telegram.notify(self.data, event="recovery", now=self.NOW)
        remote.assert_not_called()

    def test_maximal_26_blocker_cycle_still_sends_sanitized_alert(self):
        self.write(
            "health.json",
            {
                "mode": "bad",
                "running": False,
                "last_error": "x",
                "execution_authorized": True,
                "updated_at": "not-a-time",
                "last_cycle_id": "*",
                "kalshi_markets": 99,
            },
        )
        self.write(
            "packets/latest.json",
            {
                "schema_version": "bad",
                "mode": "bad",
                "execution_authorized": True,
                "generated_at": "not-a-time",
                "packet_id": "*",
                "kalshi": {"markets": [], "market_count": 0},
            },
        )
        self.write(
            "coverage.json",
            {
                "schema_version": "bad",
                "cursor_exhausted": "bad",
                "truncated_by_page_limit": "bad",
                "truncated_by_orderbook_limit": False,
                "open_markets_seen": 103,
                "eligible_in_horizon": 102,
                "orderbooks_fetched": 101,
                "generated_at": "not-a-time",
                "cycle_id": "*",
            },
        )
        self.write(
            "risk-status.json",
            {
                "schema_version": "bad",
                "bank_state": "bad",
                "execution_authorized": True,
                "order_capability_present": True,
                "real_entry_eligible": True,
                "generated_at": "not-a-time",
                "cycle_id": "*",
            },
        )

        bounded = telegram._bounded_status(
            self.data,
            state_data_dir=None,
            now=self.NOW,
        )
        self.assertEqual(bounded["technical_status"], "BLOCKED")
        self.assertEqual(len(bounded["blockers"]), 26)

        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            result = telegram.notify(self.data, now=self.NOW)
        self.assertTrue(result["delivered"])
        self.assertEqual(result["event"], "blocked")
        body = json.loads(remote.call_args.args[0].data)
        blocker_line = next(
            line for line in body["text"].splitlines() if line.startswith("Bloqueos: ")
        )
        self.assertEqual(len(blocker_line.removeprefix("Bloqueos: ").split(", ")), 26)
        self.assertLessEqual(len(body["text"]), telegram.MAX_MESSAGE_CHARS)

    def test_network_error_never_exposes_bot_token(self):
        error = HTTPError(
            f"https://api.telegram.org/bot{self.TOKEN}/sendMessage",
            500,
            f"server echoed {self.TOKEN}",
            Message(),
            None,
        )
        with patch.object(telegram.TELEGRAM_OPENER, "open", side_effect=error):
            with self.assertRaises(telegram.TelegramNotificationError) as caught:
                telegram._send_message(self.TOKEN, self.CHAT_ID, "safe")
        self.assertNotIn(self.TOKEN, str(caught.exception))

    def test_no_state_is_written_without_confirmed_delivery(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER,
            "open",
            return_value=FakeResponse({"ok": False, "description": self.TOKEN}),
        ):
            with self.assertRaisesRegex(telegram.TelegramNotificationError, "confirm delivery"):
                telegram.notify(self.data, now=self.NOW)
        self.assertFalse((self.data / "assistant/telegram/telegram-state.json").exists())

    def test_response_for_a_different_chat_is_rejected(self):
        response = FakeResponse(
            {"ok": True, "result": {"message_id": 41, "chat": {"id": -1009999999999}}}
        )
        with patch.object(telegram.TELEGRAM_OPENER, "open", return_value=response):
            with self.assertRaisesRegex(telegram.TelegramNotificationError, "unexpected chat"):
                telegram._send_message(self.TOKEN, self.CHAT_ID, "safe")

    def test_non_200_response_is_rejected_even_with_success_json(self):
        response = FakeResponse(
            {"ok": True, "result": {"message_id": 41, "chat": {"id": int(self.CHAT_ID)}}},
            status=201,
        )
        with patch.object(telegram.TELEGRAM_OPENER, "open", return_value=response):
            with self.assertRaisesRegex(telegram.TelegramNotificationError, "status"):
                telegram._send_message(self.TOKEN, self.CHAT_ID, "safe")

    def test_http_client_has_no_proxy_and_has_no_redirect_handler(self):
        proxy_handlers = [
            handler
            for handler in telegram.TELEGRAM_OPENER.handlers
            if isinstance(handler, telegram.ProxyHandler)
        ]
        redirect_handlers = [
            handler
            for handler in telegram.TELEGRAM_OPENER.handlers
            if isinstance(handler, telegram._NoRedirectHandler)
        ]
        # build_opener omits an empty ProxyHandler entirely; either representation
        # proves it did not install the environment-derived default proxy handler.
        self.assertTrue(not proxy_handlers or all(handler.proxies == {} for handler in proxy_handlers))
        self.assertEqual(len(redirect_handlers), 1)
        self.assertTrue(
            any(isinstance(handler, telegram.HTTPSHandler) for handler in telegram.TELEGRAM_OPENER.handlers)
        )
        self.assertIsNone(
            redirect_handlers[0].redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid")
        )

    def test_username_chat_target_is_rejected(self):
        environment = {**self.env, "TELEGRAM_CHAT_ID": "@mutable_channel"}
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(telegram.TelegramNotificationError, "CHAT_ID"):
                telegram.notify(self.data, now=self.NOW)

    def test_fresh_prior_cycle_assessment_is_labeled_not_presented_as_current(self):
        assessment = json.loads((self.data / "assistant/assessment-latest.json").read_text())
        assessment["source_cycle_id"] = "different-cycle"
        assessment["decision"] = "REQUEST_HUMAN_REVIEW"
        self.write("assistant/assessment-latest.json", assessment)
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            result = telegram.notify(self.data, now=self.NOW)
        self.assertTrue(result["delivered"])
        body = json.loads(remote.call_args.args[0].data)
        self.assertIn("Ciclo: cycle-telegram-1", body["text"])
        self.assertIn("Evaluación: REQUEST_HUMAN_REVIEW", body["text"])
        self.assertIn("Ciclo evaluado: different-cycle", body["text"])
        self.assertNotIn("MALICIOUS FREE TEXT", body["text"])

    def test_stale_or_future_assessment_is_not_attached(self):
        self.set_control(False)
        for created_at in (
            self.NOW - timedelta(seconds=telegram.MAX_ASSESSMENT_AGE_SECONDS + 1),
            self.NOW + timedelta(seconds=1),
        ):
            with self.subTest(created_at=created_at):
                self.set_assessment(
                    decision="REQUEST_HUMAN_REVIEW",
                    source_cycle_id="prior-cycle",
                    created_at=created_at,
                )
                observation = telegram._assessment_observation(
                    self.data,
                    current_cycle_id="cycle-telegram-1",
                    control_paused=False,
                    now=self.NOW,
                )
                self.assertEqual(observation["health"], "STALE")
                self.assertIsNone(observation["assessment"])

    def test_assessment_health_has_all_four_bounded_states(self):
        assessment_path = self.data / "assistant/assessment-latest.json"
        assessment_path.unlink()
        missing = telegram._assessment_observation(
            self.data,
            current_cycle_id="cycle-telegram-1",
            control_paused=False,
            now=self.NOW,
        )
        self.assertEqual(missing["health"], "MISSING")

        self.set_assessment(provider="offline")
        offline = telegram._assessment_observation(
            self.data,
            current_cycle_id="cycle-telegram-1",
            control_paused=False,
            now=self.NOW,
        )
        self.assertEqual(offline["health"], "OFFLINE")

        self.set_assessment(
            provider="openai",
            created_at=self.NOW - timedelta(seconds=telegram.MAX_ASSESSMENT_AGE_SECONDS + 1),
        )
        stale = telegram._assessment_observation(
            self.data,
            current_cycle_id="cycle-telegram-1",
            control_paused=False,
            now=self.NOW,
        )
        self.assertEqual(stale["health"], "STALE")

        self.set_assessment(provider="openai")
        active = telegram._assessment_observation(
            self.data,
            current_cycle_id="cycle-telegram-1",
            control_paused=False,
            now=self.NOW,
        )
        self.assertEqual(active["health"], "ACTIVE")

    def test_auto_alerts_ai_stale_then_recovery(self):
        self.set_control(False)
        self.set_assessment(provider="openai")
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            initial = telegram.notify(self.data, now=self.NOW)
            self.set_assessment(
                provider="openai",
                created_at=self.NOW - timedelta(seconds=telegram.MAX_ASSESSMENT_AGE_SECONDS + 1),
            )
            stale = telegram.notify(self.data, now=self.NOW)
            recovery_time = self.NOW + timedelta(seconds=1)
            self.set_assessment(
                provider="openai",
                assessment_id="assessment-2",
                created_at=recovery_time,
            )
            recovered = telegram.notify(self.data, now=recovery_time)

        self.assertEqual(initial["ai_health"], "ACTIVE")
        self.assertEqual(stale["event"], "ai-alert")
        self.assertEqual(stale["ai_health"], "STALE")
        self.assertEqual(recovered["event"], "ai-recovery")
        self.assertEqual(recovered["ai_health"], "ACTIVE")
        self.assertEqual(remote.call_count, 3)

    def test_pause_transition_alerts_offline(self):
        self.set_control(False)
        self.set_assessment(provider="openai")
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            telegram.notify(self.data, now=self.NOW)
            self.set_control(True)
            alert = telegram.notify(self.data, now=self.NOW)
        self.assertEqual(alert["event"], "ai-alert")
        self.assertEqual(alert["ai_health"], "OFFLINE")
        self.assertTrue(alert["control_paused"])
        self.assertEqual(remote.call_count, 2)

    def test_force_confirmation_proves_a_new_active_delivery(self):
        self.set_control(False)
        self.set_assessment(provider="openai")
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            telegram.notify(self.data, event="recovery", now=self.NOW)
            forced = telegram.notify(
                self.data,
                event="recovery",
                force_confirm="INSTALLATION_TEST",
                now=self.NOW,
            )
        self.assertTrue(forced["delivered"])
        self.assertTrue(forced["forced_confirmation"])
        self.assertEqual(forced["ai_health"], "ACTIVE")
        self.assertEqual(remote.call_count, 2)

    def test_force_confirmation_rejects_wrong_scope_or_inactive_ai(self):
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(telegram.TelegramNotificationError, "literal"):
                telegram.notify(
                    self.data,
                    event="recovery",
                    force_confirm="WRONG",
                    now=self.NOW,
                )
            with self.assertRaisesRegex(telegram.TelegramNotificationError, "active AI"):
                telegram.notify(
                    self.data,
                    event="recovery",
                    force_confirm="INSTALLATION_TEST",
                    now=self.NOW,
                )

    def test_explicit_ai_failure_deduplicates_and_auto_recovers(self):
        self.set_control(False)
        self.set_assessment(provider="openai")
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            telegram.notify(self.data, now=self.NOW)
            failed = telegram.notify(self.data, event="ai-failure", now=self.NOW)
            duplicate = telegram.notify(self.data, event="ai-failure", now=self.NOW)
            still_offline = telegram.notify(self.data, now=self.NOW)
            recovery_time = self.NOW + timedelta(seconds=1)
            self.set_assessment(
                provider="openai",
                assessment_id="assessment-2",
                created_at=recovery_time,
            )
            recovered = telegram.notify(self.data, now=recovery_time)

        self.assertEqual(failed["event"], "ai-failure")
        self.assertEqual(failed["ai_health"], "OFFLINE")
        self.assertFalse(duplicate["delivered"])
        self.assertEqual(duplicate["reason"], "DUPLICATE")
        self.assertFalse(still_offline["delivered"])
        self.assertEqual(still_offline["reason"], "UNCHANGED")
        self.assertEqual(still_offline["ai_health"], "OFFLINE")
        self.assertEqual(recovered["event"], "ai-recovery")
        self.assertEqual(recovered["ai_health"], "ACTIVE")
        self.assertEqual(remote.call_count, 3)

    def test_ai_failure_without_prior_state_requires_a_new_assessment_to_recover(self):
        self.set_control(False)
        self.set_assessment(provider="openai")
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            failed = telegram.notify(self.data, event="ai-failure", now=self.NOW)
            still_offline = telegram.notify(self.data, now=self.NOW)
            recovery_time = self.NOW + timedelta(seconds=1)
            self.set_assessment(
                provider="openai",
                assessment_id="assessment-2",
                created_at=recovery_time,
            )
            recovered = telegram.notify(self.data, now=recovery_time)

        self.assertEqual(failed["technical_status"], "BLOCKED")
        self.assertIsNone(failed["cycle_id"])
        self.assertFalse(still_offline["delivered"])
        self.assertEqual(still_offline["reason"], "UNCHANGED")
        self.assertEqual(still_offline["ai_health"], "OFFLINE")
        self.assertEqual(recovered["event"], "ai-recovery")
        self.assertEqual(recovered["ai_health"], "ACTIVE")
        self.assertEqual(remote.call_count, 2)

    def test_state_updated_at_must_be_valid_and_not_future(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ):
            telegram.notify(self.data, now=self.NOW)
        state_path = self.data / "assistant/telegram/telegram-state.json"
        original = json.loads(state_path.read_text(encoding="utf-8"))

        for invalid_time in ("not-a-time", (self.NOW + timedelta(seconds=1)).isoformat()):
            with self.subTest(updated_at=invalid_time):
                state = {**original, "updated_at": invalid_time}
                state_path.write_text(json.dumps(state), encoding="utf-8")
                with patch.dict(os.environ, self.env, clear=True), patch.object(
                    telegram.TELEGRAM_OPENER, "open"
                ) as remote:
                    with self.assertRaisesRegex(
                        telegram.TelegramNotificationError,
                        "state time is invalid",
                    ):
                        telegram.notify(self.data, now=self.NOW)
                remote.assert_not_called()

    def test_explicit_state_data_separates_inputs_and_writes(self):
        with tempfile.TemporaryDirectory() as state_tmp:
            state_root = Path(state_tmp)
            self.set_control(False, root=state_root)
            self.set_assessment(provider="openai", root=state_root)
            with patch.dict(os.environ, self.env, clear=True), patch.object(
                telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
            ):
                result = telegram.notify(
                    self.data,
                    state_data_dir=state_root,
                    now=self.NOW,
                )
            self.assertEqual(result["ai_health"], "ACTIVE")
            self.assertTrue((state_root / "telegram/telegram-state.json").is_file())
            self.assertFalse((self.data / "assistant/telegram/telegram-state.json").exists())

    def test_exclusive_lock_serializes_a_concurrent_send(self):
        lock = self.data / "assistant/telegram/notify.lock"
        lock.parent.mkdir(parents=True)
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        released = threading.Event()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release_lock():
                time.sleep(0.05)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                released.set()

            thread = threading.Thread(target=release_lock)
            thread.start()
            with patch.dict(os.environ, self.env, clear=True), patch.object(
                telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
            ) as remote:
                result = telegram.notify(self.data, now=self.NOW)
            thread.join(timeout=1)
            self.assertTrue(released.is_set())
            self.assertTrue(result["delivered"])
            remote.assert_called_once()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_exclusive_lock_wait_is_bounded(self):
        lock = self.data / "assistant/telegram/notify.lock"
        lock.parent.mkdir(parents=True)
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.dict(os.environ, self.env, clear=True), patch.object(
                telegram, "LOCK_WAIT_SECONDS", 0.01
            ), patch.object(telegram.TELEGRAM_OPENER, "open") as remote:
                started = time.monotonic()
                with self.assertRaisesRegex(telegram.TelegramNotificationError, "timed out"):
                    telegram.notify(self.data, now=self.NOW)
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1)
            remote.assert_not_called()
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def test_lock_wait_covers_one_network_timeout(self):
        self.assertGreater(
            telegram.LOCK_WAIT_SECONDS,
            telegram.TELEGRAM_TIMEOUT_SECONDS,
        )

    def test_corrupt_delivery_state_fails_closed_before_network(self):
        self.write("assistant/telegram/telegram-state.json", {"schema_version": "wrong"})
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open"
        ) as remote:
            with self.assertRaisesRegex(telegram.TelegramNotificationError, "schema"):
                telegram.notify(self.data, now=self.NOW)
        remote.assert_not_called()

    def test_forbidden_credentials_and_execution_flags_are_rejected(self):
        for extra in ({"KALSHI_API_KEY_ID": "secret"}, {"TRADING_ENABLED": "true"}):
            environment = {**self.env, **extra}
            with self.subTest(extra=tuple(extra)), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(telegram.TelegramNotificationError):
                    telegram.notify(self.data, now=self.NOW)

    def test_systemd_credentials_are_used_without_exposing_values(self):
        with tempfile.TemporaryDirectory() as credentials_tmp:
            credentials = Path(credentials_tmp)
            (credentials / "telegram_bot_token").write_text(self.TOKEN + "\n", encoding="utf-8")
            (credentials / "telegram_chat_id").write_text(self.CHAT_ID + "\n", encoding="utf-8")
            environment = {
                "CREDENTIALS_DIRECTORY": str(credentials),
                "TELEGRAM_BOT_TOKEN": "environment-must-not-be-used",
                "TELEGRAM_CHAT_ID": "also-not-used",
                "TRADING_ENABLED": "false",
            }
            with patch.dict(os.environ, environment, clear=True), patch.object(
                telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
            ) as remote:
                result = telegram.notify(self.data, now=self.NOW)
        self.assertTrue(result["delivered"])
        request = remote.call_args.args[0]
        self.assertIn(self.TOKEN, request.full_url)
        self.assertNotIn("environment-must-not-be-used", request.full_url)

    def test_configured_credential_directory_never_falls_back_to_environment(self):
        with tempfile.TemporaryDirectory() as credentials_tmp:
            environment = {**self.env, "CREDENTIALS_DIRECTORY": credentials_tmp}
            with patch.dict(os.environ, environment, clear=True), patch.object(
                telegram.TELEGRAM_OPENER, "open"
            ) as remote:
                with self.assertRaisesRegex(
                    telegram.TelegramNotificationError,
                    "telegram_bot_token credential is missing",
                ):
                    telegram.notify(self.data, now=self.NOW)
        remote.assert_not_called()

    def test_assessment_authority_escalation_fails_before_network(self):
        assessment = json.loads((self.data / "assistant/assessment-latest.json").read_text())
        assessment["execution_authorized"] = True
        self.write("assistant/assessment-latest.json", assessment)
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open"
        ) as remote:
            with self.assertRaisesRegex(telegram.TelegramNotificationError, "assessment"):
                telegram.notify(self.data, now=self.NOW)
        remote.assert_not_called()

    def test_ai_failure_never_reads_or_relays_corrupt_assessment(self):
        assessment = json.loads((self.data / "assistant/assessment-latest.json").read_text())
        assessment["execution_authorized"] = True
        assessment["summary"] = "SECRET FAILURE DETAILS"
        self.write("assistant/assessment-latest.json", assessment)
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ) as remote:
            result = telegram.notify(self.data, event="ai-failure", now=self.NOW)
        self.assertTrue(result["delivered"])
        body = json.loads(remote.call_args.args[0].data)
        self.assertIn("ASSISTANT_SERVICE_FAILED", body["text"])
        self.assertNotIn("SECRET FAILURE DETAILS", body["text"])
        self.assertNotIn("Evaluación:", body["text"])

    def test_ai_failure_does_not_read_research_or_assessment_inputs(self):
        with tempfile.TemporaryDirectory() as state_tmp:
            state_root = Path(state_tmp)
            self.set_control(False, root=state_root)
            unavailable_research = self.data / "research-not-mounted"
            with patch.dict(os.environ, self.env, clear=True), patch.object(
                telegram, "build_status", side_effect=AssertionError("research read")
            ) as build, patch.object(
                telegram, "latest_assessment", side_effect=AssertionError("assessment read")
            ) as assessment, patch.object(
                telegram.TELEGRAM_OPENER,
                "open",
                return_value=self.delivered_response(),
            ) as remote:
                result = telegram.notify(
                    unavailable_research,
                    state_data_dir=state_root,
                    event="ai-failure",
                    now=self.NOW,
                )

        self.assertTrue(result["delivered"])
        self.assertEqual(result["technical_status"], "BLOCKED")
        self.assertIsNone(result["cycle_id"])
        self.assertEqual(result["ai_health"], "OFFLINE")
        self.assertFalse(result["control_paused"])
        build.assert_not_called()
        assessment.assert_not_called()
        body = json.loads(remote.call_args.args[0].data)
        self.assertIn("Estado técnico: BLOCKED", body["text"])
        self.assertIn("Ciclo: no verificado", body["text"])
        self.assertIn("Cobertura: UNVERIFIED", body["text"])
        self.assertIn("ASSISTANT_SERVICE_FAILED", body["text"])
        self.assertNotIn("_UNAVAILABLE", body["text"])

    def test_atomic_state_is_private_regular_file(self):
        with patch.dict(os.environ, self.env, clear=True), patch.object(
            telegram.TELEGRAM_OPENER, "open", return_value=self.delivered_response()
        ):
            telegram.notify(self.data, now=self.NOW)
        state = self.data / "assistant/telegram/telegram-state.json"
        self.assertTrue(state.is_file())
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
