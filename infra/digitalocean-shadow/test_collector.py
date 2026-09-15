import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("c", Path(__file__).with_name("collector.py"))
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

class T(unittest.TestCase):
    def test_rejects_kalshi_credentials(self):
        with self.assertRaises(c.ResearchError):
            c.safety_check({"KALSHI_PRIVATE_KEY": "x"})

    def test_rejects_execution(self):
        with self.assertRaises(c.ResearchError):
            c.safety_check({"TRADING_ENABLED": "true"})

    def test_accepts_shadow(self):
        c.safety_check({"TRADING_ENABLED": "false"})

    def test_origin_allowlist(self):
        r = c.Reader(); r.opener = Mock()
        with self.assertRaises(c.ResearchError):
            r.get_json("https://evil.invalid", "/x")
        r.opener.open.assert_not_called()

    def test_numeric_book_sanitized(self):
        x = c.numeric_levels({"yes_dollars": [["0.51", "5.50"], ["x", "1"]], "noise": "secret"})
        self.assertEqual(x, {"yes_dollars": [["0.51", "5.50"]]})
        self.assertNotIn("noise", x)

    def test_odds_sanitized(self):
        e = c.sanitize_odds_event({
            "id":"e", "sport_key":"baseball_mlb", "home_team":"A", "away_team":"B",
            "bookmakers":[{"key":"fd", "markets":[{"key":"totals", "outcomes":[{"name":"Over", "price":1.9, "point":7.5}]}]}],
            "extra":"drop"
        })
        self.assertEqual(e["id"], "e")
        self.assertNotIn("extra", e)

if __name__ == "__main__":
    unittest.main()
