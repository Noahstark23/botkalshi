import importlib
from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
runner = importlib.import_module("research_runner")


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


if __name__ == "__main__":
    unittest.main()
