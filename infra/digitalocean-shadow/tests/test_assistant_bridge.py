from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
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

    def test_offline_assessment_writes_bounded_no_action(self):
        bridge.set_pause(self.data, paused=False, reason="simulation only", actor="operator")
        result = bridge.assess(self.data, provider="offline", now=self.NOW)
        self.assertEqual(result["decision"], "NO_ACTION")
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["commands_executed"], [])
        self.assertTrue((self.data / "assistant" / "assessment-latest.json").is_file())

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
