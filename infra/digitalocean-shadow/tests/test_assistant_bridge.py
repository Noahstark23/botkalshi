from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location("assistant_bridge", BASE / "assistant_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class AssistantBridgeTests(unittest.TestCase):
    NOW = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)

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
                "last_cycle_id": "cycle-1",
                "kalshi_markets": 1,
                "execution_authorized": False,
            },
        )
        self.write(
            "packets/latest.json",
            {
                "schema_version": "botkalshi-research-packet-v1",
                "packet_id": "cycle-1",
                "generated_at": when,
                "mode": "SHADOW_READONLY",
                "execution_authorized": False,
                "kalshi": {
                    "market_count": 1,
                    "markets": [
                        {
                            "ticker": "SYN-1",
                            "close_time": "2026-09-20T20:00:00Z",
                            "levels": {"yes_dollars": [["0.50", "2.00"]]},
                        }
                    ],
                },
                "sportsbook": {"provider": "test", "status": "NOT_CONFIGURED", "event_count": 0},
            },
        )
        self.write(
            "coverage.json",
            {
                "schema_version": "botkalshi-coverage-v2",
                "cycle_id": "cycle-1",
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
                "cycle_id": "cycle-1",
                "generated_at": when,
                "bank_state": "PENDING_RECONCILIATION",
                "execution_authorized": False,
                "order_capability_present": False,
                "real_entry_eligible": False,
            },
        )

    def write(self, relative, value):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_default_control_is_paused_fail_closed(self):
        status = bridge.build_status(self.data, now=self.NOW)
        self.assertTrue(status["control"]["paused"])
        self.assertFalse(status["capabilities"]["place_order"])

    def test_operator_can_resume_assessment_not_trading(self):
        state = bridge.set_pause(
            self.data,
            paused=False,
            reason="simulation only",
            actor="operator",
        )
        self.assertFalse(state["paused"])
        self.assertFalse(state["trading_state_changed"])

    def test_integration_state_can_be_isolated_from_collector_data(self):
        state_data = Path(self.tmp.name + "-integration-state")
        self.addCleanup(shutil.rmtree, state_data, True)
        bridge.set_pause(
            self.data,
            paused=False,
            reason="simulation only",
            actor="operator",
            state_data_dir=state_data,
        )
        result = bridge.assess(
            self.data,
            provider="offline",
            now=self.NOW,
            state_data_dir=state_data,
        )
        status = bridge.build_status(
            self.data,
            now=self.NOW,
            state_data_dir=state_data,
        )
        latest = bridge.latest_assessment(self.data, state_data_dir=state_data)

        self.assertFalse((self.data / "assistant").exists())
        self.assertTrue((state_data / "assistant" / "control-state.json").is_file())
        self.assertTrue((state_data / "assistant" / "assessment-latest.json").is_file())
        self.assertEqual(result["assessment_id"], latest["assessment_id"])
        self.assertFalse(status["control"]["paused"])

    def test_offline_assessment_writes_bounded_no_action(self):
        bridge.set_pause(self.data, paused=False, reason="simulation only", actor="operator")
        result = bridge.assess(self.data, provider="offline", now=self.NOW)
        self.assertEqual(result["decision"], "NO_ACTION")
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["commands_executed"], [])
        self.assertTrue((self.data / "assistant" / "assessment-latest.json").is_file())

    def test_snapshot_is_verified_bounded_and_non_executing(self):
        result = bridge.build_snapshot(self.data, now=self.NOW)
        self.assertEqual(result["cycle"]["technical_status"], "VERIFIED")
        self.assertEqual(result["snapshot"]["market_sample"][0]["ticker"], "SYN-1")
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["order_capability_present"])

    def test_latest_assessment_rejects_authority_escalation(self):
        self.write(
            "assistant/assessment-latest.json",
            {
                "schema_version": "botkalshi-assistant-assessment-v1",
                "execution_authorized": True,
                "order_capability_present": False,
            },
        )
        with self.assertRaises(bridge.BridgeError):
            bridge.latest_assessment(self.data)

    def test_blocked_cycle_never_calls_external_provider(self):
        packet = json.loads((self.data / "packets/latest.json").read_text())
        packet["packet_id"] = "other"
        self.write("packets/latest.json", packet)
        with patch.object(bridge, "_openai_assessment") as remote:
            result = bridge.assess(self.data, provider="openai", model="not-used", now=self.NOW)
        remote.assert_not_called()
        self.assertEqual(result["provider"], "local-fail-closed")

    def test_ai_process_rejects_kalshi_credentials(self):
        with patch.dict(os.environ, {"KALSHI_API_KEY_ID": "secret"}, clear=True):
            with self.assertRaises(bridge.BridgeError):
                bridge._assert_ai_environment()

    def test_assessment_validator_rejects_order_command(self):
        value = {
            "decision": "NO_ACTION",
            "confidence": 1.0,
            "summary": "safe",
            "evidence_ids": [],
            "blockers": [],
            "proposed_commands": ["PLACE_ORDER"],
        }
        with self.assertRaises(bridge.BridgeError):
            bridge._validate_assessment(value)

    def test_openai_response_must_be_completed_and_not_a_refusal(self):
        with self.assertRaisesRegex(bridge.BridgeError, "not an object"):
            bridge._extract_output_text([])
        with self.assertRaisesRegex(bridge.BridgeError, "not completed"):
            bridge._extract_output_text({"status": "incomplete", "output": []})
        with self.assertRaisesRegex(bridge.BridgeError, "refused"):
            bridge._extract_output_text(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "refusal", "refusal": "not returned"}],
                        }
                    ],
                }
            )

    def test_openai_uses_systemd_credential_and_strict_nonstored_output(self):
        credentials = self.data / "credentials"
        credentials.mkdir()
        (credentials / "openai_api_key").write_text("sk-test-only\n", encoding="utf-8")
        assessment = {
            "decision": "NO_ACTION",
            "confidence": 1.0,
            "summary": "safe",
            "evidence_ids": ["cycle-1"],
            "blockers": [],
            "proposed_commands": ["NOOP"],
        }
        response_body = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(assessment)}
                    ],
                }
            ],
        }
        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                self.limit = limit
                return json.dumps(response_body).encode("utf-8")

        def fake_open(request, *, timeout):
            captured["payload"] = json.loads(request.data)
            captured["authorization"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.dict(
            os.environ,
            {"CREDENTIALS_DIRECTORY": str(credentials)},
            clear=True,
        ), patch.object(bridge, "_open_https", side_effect=fake_open):
            result = bridge._openai_assessment({"cycle": "cycle-1"}, model="test-model")

        self.assertEqual(result, assessment)
        self.assertEqual(captured["authorization"], "Bearer sk-test-only")
        self.assertIs(captured["payload"]["store"], False)
        self.assertIs(captured["payload"]["text"]["format"]["strict"], True)
        self.assertEqual(captured["timeout"], 45)

    def test_configured_credential_directory_does_not_fall_back_to_environment(self):
        credentials = self.data / "empty-credentials"
        credentials.mkdir()
        with patch.dict(
            os.environ,
            {
                "CREDENTIALS_DIRECTORY": str(credentials),
                "OPENAI_API_KEY": "must-not-be-used",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(bridge.BridgeError, "credential is missing"):
                bridge._read_credential("OPENAI_API_KEY", "openai_api_key")

    def test_openai_http_client_disables_proxies_and_redirects(self):
        opener = unittest.mock.MagicMock()
        with patch.object(bridge, "build_opener", return_value=opener) as builder:
            bridge._open_https(bridge.Request(bridge.OPENAI_ENDPOINT), timeout=12)
        handlers = builder.call_args.args
        proxy_handlers = [item for item in handlers if isinstance(item, bridge.ProxyHandler)]
        redirect_handlers = [item for item in handlers if isinstance(item, bridge._NoRedirect)]
        self.assertTrue(not proxy_handlers or all(item.proxies == {} for item in proxy_handlers))
        self.assertEqual(len(redirect_handlers), 1)
        self.assertIsNone(
            redirect_handlers[0].redirect_request(
                None,
                None,
                302,
                "redirect",
                {},
                "https://untrusted.invalid",
            )
        )
        opener.open.assert_called_once()
