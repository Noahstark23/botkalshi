import importlib
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
runner = importlib.import_module("research_runner")
verifier = importlib.import_module("cycle_verifier")


class FakeReader:
    def get_json(self, origin, path, params=None):
        if path == "/markets":
            return {
                "markets": [
                    {
                        "ticker": "SYNTHETIC-1",
                        "event_ticker": "SYNTHETIC",
                        "status": "open",
                        "close_time": "2026-09-20T20:00:00Z",
                    }
                ],
                "cursor": "",
            }
        return {"orderbook": {"yes": [[50, 2]]}}


class RunnerSafetyTests(unittest.TestCase):
    def test_collector_still_rejects_kalshi_credentials(self):
        with self.assertRaises(runner.collector.ResearchError):
            runner.collector.safety_check({"KALSHI_API_KEY_ID": "should-fail"})

    def test_collector_still_rejects_execution_flags(self):
        with self.assertRaises(runner.collector.ResearchError):
            runner.collector.safety_check({"TRADING_ENABLED": "true"})

    def test_risk_layer_has_no_execution_authority(self):
        status = runner.write_status
        self.assertTrue(callable(status))
        self.assertFalse(hasattr(runner, "place_order"))
        self.assertFalse(hasattr(runner, "submit_order"))

    def test_one_cycle_links_all_four_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {
                "TRADING_ENABLED": "false",
                "BOTKALSHI_MAX_ORDERBOOKS": "100",
                "BOTKALSHI_HORIZON_HOURS": "72",
            },
            clear=True,
        ):
            old_dir = runner.collector.DATA_DIR
            old_collect = runner.collector.collect_kalshi
            runner.collector.DATA_DIR = Path(tmp)
            runner.collector.collect_kalshi = runner._collect_kalshi
            try:
                con = runner.collector.open_db()
                try:
                    runner._cycle_with_risk(FakeReader(), con, (0.0, [], "NOT_CONFIGURED"))
                finally:
                    con.close()
                paths = (
                    "health.json",
                    "packets/latest.json",
                    "coverage.json",
                    "risk-status.json",
                )
                health, packet, coverage, risk = [
                    json.loads((Path(tmp) / path).read_text(encoding="utf-8"))
                    for path in paths
                ]
                result = verifier.verify_cycle(
                    health,
                    packet,
                    coverage,
                    risk,
                    now=datetime.now(UTC),
                )
                self.assertEqual(result["technical_status"], "VERIFIED")
                self.assertEqual(result["cycle_id"], packet["packet_id"])
                self.assertFalse(result["execution_authorized"])
            finally:
                runner.collector.DATA_DIR = old_dir
                runner.collector.collect_kalshi = old_collect


if __name__ == "__main__":
    unittest.main()
