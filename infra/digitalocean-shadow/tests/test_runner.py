import importlib
from datetime import UTC, datetime, timedelta
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

# Anchored to a single captured instant (patched into runner._collection_now
# below), never a hardcoded date string — a fixed past-looking string like
# "2026-09-20T..." eventually falls behind wall-clock `now()` and pagination's
# horizon filter would silently drop every fixture market (real regression
# hit during supervisor validation). Deriving CLOSE_TIME from the same
# instant that gets patched into the collection clock keeps it future
# relative to that clock forever, regardless of when the suite actually runs.
COLLECTION_NOW = datetime.now(UTC)
CLOSE_TIME = (COLLECTION_NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z")


class FakeReader:
    def get_json(self, origin, path, params=None):
        if path == "/markets":
            return {
                "markets": [
                    {
                        "ticker": "SYNTHETIC-1",
                        "event_ticker": "SYNTHETIC",
                        "status": "open",
                        "close_time": CLOSE_TIME,
                    }
                ],
                "cursor": "",
            }
        return {"orderbook": {"yes": [[50, 2]]}}


class DollarBookReader:
    """Book with fixed-point yes_dollars/no_dollars, the only levels M1 may use."""

    def get_json(self, origin, path, params=None):
        if path == "/markets":
            return {
                "markets": [
                    {
                        "ticker": "SYNTHETIC-2",
                        "event_ticker": "SYNTHETIC",
                        "status": "open",
                        "close_time": CLOSE_TIME,
                    }
                ],
                "cursor": "",
            }
        return {"orderbook_fp": {"yes_dollars": [["0.4500", "10"]], "no_dollars": [["0.4000", "7"]]}}


class PartiallyInvalidDollarBookReader:
    """One malformed row mixed with an otherwise-valid yes_dollars side.

    Must never let review_m1_book see the good row alone as if it were the
    whole book — a dropped-and-cleaned subset could hide a real crossed/
    incoherent book behind a NO_ARBITRAGE verdict.
    """

    def get_json(self, origin, path, params=None):
        if path == "/markets":
            return {
                "markets": [
                    {
                        "ticker": "SYNTHETIC-3",
                        "event_ticker": "SYNTHETIC",
                        "status": "open",
                        "close_time": CLOSE_TIME,
                    }
                ],
                "cursor": "",
            }
        return {"orderbook_fp": {
            "yes_dollars": [["0.4500", "10"], [0.30, "5"]],
            "no_dollars": [["0.4000", "7"]],
        }}


class SubcentDollarBookReader:
    """Exact sub-cent fraction strings that must reach review_m1_book unchanged."""

    def get_json(self, origin, path, params=None):
        if path == "/markets":
            return {
                "markets": [
                    {
                        "ticker": "SYNTHETIC-4",
                        "event_ticker": "SYNTHETIC",
                        "status": "open",
                        "close_time": CLOSE_TIME,
                    }
                ],
                "cursor": "",
            }
        return {"orderbook_fp": {
            "yes_dollars": [["0.1234", "3.50"]],
            "no_dollars": [["0.2345", "4.25"]],
        }}


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
                    with patch.object(runner, "_collection_now", return_value=COLLECTION_NOW):
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

    def _run_cycle(self, reader):
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
                    with patch.object(runner, "_collection_now", return_value=COLLECTION_NOW):
                        runner._cycle_with_risk(reader, con, (0.0, [], "NOT_CONFIGURED"))
                finally:
                    con.close()
                packet = json.loads((Path(tmp) / "packets" / "latest.json").read_text(encoding="utf-8"))
                m1 = json.loads((Path(tmp) / "m1" / "latest.json").read_text(encoding="utf-8"))
            finally:
                runner.collector.DATA_DIR = old_dir
                runner.collector.collect_kalshi = old_collect
        return packet, m1

    def test_m1_review_is_a_separate_artifact_linked_to_cycle_id(self):
        packet, m1 = self._run_cycle(FakeReader())
        self.assertEqual(m1["schema_version"], runner.M1_CYCLE_REVIEW_SCHEMA)
        self.assertEqual(m1["cycle_id"], packet["packet_id"])
        self.assertEqual(m1["market_count"], len(packet["kalshi"]["markets"]))
        self.assertIsNone(packet["assessment"])
        for review in m1["reviews"]:
            self.assertEqual(review["mode"], "SIMULATION_ONLY")
            self.assertFalse(review["execution_authorized"])
            self.assertEqual(review["cycle_id"], packet["packet_id"])

    def test_m1_review_fails_closed_without_dollar_levels_instead_of_inventing_data(self):
        # FakeReader's book only has integer-cent "yes" levels — no yes_dollars/
        # no_dollars means M1 must record NO_DATA, never a fabricated zero price.
        _, m1 = self._run_cycle(FakeReader())
        self.assertEqual(m1["reviews"][0]["status"], "NO_DATA")
        self.assertIn("EMPTY_BOOK_SIDE", m1["reviews"][0]["reason_codes"])

    def test_m1_review_uses_fixed_point_dollar_levels_when_present(self):
        _, m1 = self._run_cycle(DollarBookReader())
        review = m1["reviews"][0]
        self.assertEqual(review["status"], "NO_ARBITRAGE")
        self.assertTrue(review["legacy_detector_called"])
        self.assertEqual(review["best_yes_bid"], {"price_usd": "0.4500", "quantity_contracts": "10"})
        self.assertEqual(review["best_no_bid"], {"price_usd": "0.4000", "quantity_contracts": "7"})

    def test_m1_review_never_yields_no_arbitrage_from_a_malformed_row_mixed_with_valid_rows(self):
        # numeric_levels (the general `levels` field) would stringify the bad
        # row's non-string price and hand review_m1_book what still looks
        # like a plausible two-row book. The strict m1_book capture must
        # instead see the raw non-string value and fail the whole book
        # closed -- never NO_ARBITRAGE from a book that was partially bad.
        _, m1 = self._run_cycle(PartiallyInvalidDollarBookReader())
        review = m1["reviews"][0]
        self.assertNotEqual(review["status"], "NO_ARBITRAGE")
        self.assertEqual(review["status"], "NO_DATA")
        self.assertIn("EMPTY_BOOK_SIDE", review["reason_codes"])

    def test_m1_review_preserves_exact_subcent_fraction_strings(self):
        # These prices are genuinely sub-cent (not multiples of $0.01), so
        # review_m1_book itself fails closed on legacy-detector precision --
        # NOT_EVALUATED, not NO_ARBITRAGE. The point of this regression is
        # that best_yes_bid/best_no_bid still carry the exact input strings
        # through pagination -> research_runner -> review_m1_book, never
        # floored, truncated, or re-stringified from a float along the way.
        _, m1 = self._run_cycle(SubcentDollarBookReader())
        review = m1["reviews"][0]
        self.assertEqual(review["status"], "NOT_EVALUATED")
        self.assertIn("UNSUPPORTED_LEGACY_PRECISION", review["reason_codes"])
        self.assertFalse(review["legacy_detector_called"])
        self.assertEqual(review["best_yes_bid"], {"price_usd": "0.1234", "quantity_contracts": "3.50"})
        self.assertEqual(review["best_no_bid"], {"price_usd": "0.2345", "quantity_contracts": "4.25"})


if __name__ == "__main__":
    unittest.main()
