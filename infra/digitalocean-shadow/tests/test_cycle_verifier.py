from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location("cycle_verifier", BASE / "cycle_verifier.py")
cv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cv)


class CycleVerifierTests(unittest.TestCase):
    NOW = datetime(2026, 9, 20, 16, 0, tzinfo=UTC)

    def setUp(self):
        when = (self.NOW - timedelta(seconds=10)).isoformat()
        self.health = {
            "running": True,
            "updated_at": when,
            "mode": "SHADOW_READONLY",
            "last_cycle_id": "cycle-1",
            "kalshi_markets": 1,
            "execution_authorized": False,
        }
        self.packet = {
            "schema_version": "botkalshi-research-packet-v1",
            "packet_id": "cycle-1",
            "generated_at": when,
            "mode": "SHADOW_READONLY",
            "execution_authorized": False,
            "kalshi": {"market_count": 1, "markets": [{"ticker": "SYNTHETIC-1"}]},
        }
        self.coverage = {
            "schema_version": "botkalshi-coverage-v2",
            "cycle_id": "cycle-1",
            "generated_at": when,
            "open_markets_seen": 1,
            "eligible_in_horizon": 1,
            "orderbooks_fetched": 1,
            "cursor_exhausted": True,
            "truncated_by_page_limit": False,
            "truncated_by_orderbook_limit": False,
        }
        self.risk = {
            "schema_version": "botkalshi-risk-status-v2",
            "cycle_id": "cycle-1",
            "generated_at": when,
            "bank_state": "PENDING_RECONCILIATION",
            "execution_authorized": False,
            "order_capability_present": False,
            "real_entry_eligible": False,
        }

    def verify(self):
        return cv.verify_cycle(
            self.health,
            self.packet,
            self.coverage,
            self.risk,
            now=self.NOW,
        )

    def test_coherent_cycle_is_technical_only(self):
        result = self.verify()
        self.assertEqual(result["technical_status"], "VERIFIED")
        self.assertFalse(result["bank_reconciled"])
        self.assertFalse(result["execution_authorized"])

    def test_each_artifact_identity_mismatch_blocks(self):
        cases = (
            (self.health, "last_cycle_id"),
            (self.packet, "packet_id"),
            (self.coverage, "cycle_id"),
            (self.risk, "cycle_id"),
        )
        for artifact, field in cases:
            original = artifact[field]
            artifact[field] = "other-cycle"
            with self.subTest(field=field):
                self.assertIn("CYCLE_MISMATCH", self.verify()["blockers"])
            artifact[field] = original

    def test_stale_packet_not_cured_by_fresh_health(self):
        self.packet["generated_at"] = (self.NOW - timedelta(minutes=5)).isoformat()
        self.assertIn("PACKET_STALE_OR_FUTURE", self.verify()["blockers"])

    def test_partial_100_of_150_remains_partial(self):
        self.packet["kalshi"] = {
            "market_count": 100,
            "markets": [{"ticker": f"SYN-{n}"} for n in range(100)],
        }
        self.health["kalshi_markets"] = 100
        self.coverage.update(
            open_markets_seen=150,
            eligible_in_horizon=150,
            orderbooks_fetched=100,
            truncated_by_orderbook_limit=True,
        )
        result = self.verify()
        self.assertEqual(result["technical_status"], "VERIFIED")
        self.assertEqual(result["coverage_status"], "PARTIAL")

    def test_zero_is_valid_but_count_mismatch_blocks(self):
        self.packet["kalshi"] = {"market_count": 0, "markets": []}
        self.health["kalshi_markets"] = 0
        self.coverage.update(
            open_markets_seen=0,
            eligible_in_horizon=0,
            orderbooks_fetched=0,
        )
        self.assertEqual(self.verify()["technical_status"], "VERIFIED")
        self.coverage["orderbooks_fetched"] = 1
        self.assertIn("COVERAGE_PACKET_COUNT_MISMATCH", self.verify()["blockers"])

    def test_health_count_and_coverage_flags_are_part_of_contract(self):
        self.health["kalshi_markets"] = 2
        self.assertIn("HEALTH_PACKET_COUNT_MISMATCH", self.verify()["blockers"])
        self.health["kalshi_markets"] = 1
        self.coverage.pop("cursor_exhausted")
        self.assertIn("COVERAGE_FLAGS_INVALID", self.verify()["blockers"])

    def test_authority_must_be_literal_false(self):
        for value in (True, None, "false", 0):
            self.risk["real_entry_eligible"] = value
            with self.subTest(value=value):
                self.assertIn(
                    "RISK_REAL_ENTRY_ELIGIBLE_NOT_FALSE",
                    self.verify()["blockers"],
                )
