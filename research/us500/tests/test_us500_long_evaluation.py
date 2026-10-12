"""Sesión sintética larga: continuidad contable, límites y reinicio offline."""

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_us500_session import change_market, shift_snapshot

from example_session import session
from us500_engine import DataError
from us500_ledger import PaperLedger
from us500_long_evaluation import SCHEMA, assemble, evaluate
from us500_runner import build_plan
from us500_session import load_package


def bundle(count=7141, sizes=(1500, 1500, 1500, 1500, 1141)):
    # Posición cruza part01->02, part02->03 y part03->04; funding explícito
    # exactamente en el borde, costes en todos los frames. Son datos inventados.
    prices = ["13"] * count
    for index in range(1498, min(6001, count)):
        prices[index] = str(Decimal("13") + Decimal(index - 1497) / 10000)
    base = session(prices)
    base["ledger"]["max_gap_ms"] = 120_000
    base["strategy"]["assumptions_note"] = (
        "Synthetic 60s fixture; every cost and price is invented."
    )
    base["strategy"]["threshold_bps"] = "0.001"
    captures = []
    costs = []
    previous = None
    for index, frame in enumerate(base["frames"]):
        snap = frame["snapshot"]
        shift_snapshot(snap, index * 59_000)
        frame["at_ms"] += index * 59_000
        change_market(
            snap,
            market_id="synthetic-long",
            market_version=1,
            exchange_index=1,
            asset_class="Indices",
        )
        captures.append(snap)
        at = frame["at_ms"]
        costs.append(
            {
                "from_ms": previous or at,
                "at_ms": at,
                "funding_cashflow_usd": "-0.01"
                if len(costs) in (1500, 3000, 4500)
                else "0",
                "infrastructure_cost_usd": "0.001",
                "provenance_note": "Test sintético; costes inventados, no cuenta real.",
            }
        )
        previous = at
    segments = []
    offset = 0
    for i, size in enumerate(sizes, 1):
        segments.append(
            {
                "name": f"part{i:02d}",
                "snapshots": captures[offset : offset + size],
                "interval_costs": costs[offset : offset + size],
            }
        )
        offset += size
    return {
        "schema": SCHEMA,
        "run_id": "synthetic-long-v1",
        "ledger": base["ledger"],
        "strategy": base["strategy"],
        "segments": segments,
    }


class LongEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "paper.sqlite"
        guard = patch("socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_7141_frames_one_bank_same_events_after_restart_and_large_file_loading(
        self,
    ):
        data = bundle()
        package = Path(self.tmp.name) / "bundle.json"
        package.write_text(json.dumps(data))
        self.assertGreater(package.stat().st_size, 2_000_000)
        report = evaluate(load_package(package), self.db)
        self.assertEqual(len(report["frames"]), 7141)
        costs = [c for s in data["segments"] for c in s["interval_costs"]]
        self.assertEqual(costs[-1]["at_ms"] - costs[0]["at_ms"], 119 * 3_600_000)
        self.assertEqual(report["input_provenance"]["requested_frames"], 7141)
        self.assertEqual(Decimal(report["final_state"]["costs_usd"]), Decimal("7.141"))
        self.assertEqual(
            Decimal(report["final_state"]["funding_usd"]), Decimal("-0.03")
        )
        self.assertEqual(report["capital_resets_between_segments"], 0)
        for edge in (1500, 3000, 4500):
            self.assertNotEqual(
                Decimal(report["frames"][edge]["state"]["position_quantity"]), 0
            )
        second = evaluate(data, self.db)
        self.assertEqual(report, second)
        with closing(sqlite3.connect(f"{self.db.as_uri()}?mode=ro", uri=True)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM ledger_events").fetchone()[0],
                report["ledger_events"],
            )
        self.assertFalse(report["strategy_validated"])
        self.assertFalse(report["input_provenance"]["complete_market_session_verified"])
        self.assertEqual(report["actual_orders"], 0)

    def test_boundaries_do_not_reset_signal_or_change_monolithic_accounting(self):
        data = bundle(10, (2, 3, 5))
        assembled = assemble(data)
        _, a = build_plan(assembled)
        mono = copy.deepcopy(assembled)
        mono.pop("evaluation_segments")
        _, b = build_plan(mono)
        self.assertEqual(a["frames"], b["frames"])
        self.assertEqual(a["final_state"], b["final_state"])

    def test_missing_cost_or_reset_interval_gap_overlap_and_changed_identity_reject(
        self,
    ):
        for mutation in (
            "missing_cost",
            "reset_interval",
            "gap",
            "overlap",
            "identity",
            "order",
            "count",
        ):
            data = bundle(10, (5, 5))
            segment = data["segments"][1]
            if mutation == "missing_cost":
                segment["interval_costs"].pop()
            elif mutation == "reset_interval":
                segment["interval_costs"][0]["from_ms"] = segment["interval_costs"][0][
                    "at_ms"
                ]
            elif mutation == "gap":
                data["ledger"]["max_gap_ms"] = 500
            elif mutation == "overlap":
                segment["snapshots"][0] = copy.deepcopy(
                    data["segments"][0]["snapshots"][-1]
                )
            elif mutation == "identity":
                change_market(segment["snapshots"][0], market_id="other")
            elif mutation == "order":
                data["segments"].reverse()
            elif mutation == "count":
                segment["snapshots"] *= 2000
                segment["interval_costs"] *= 2000
            with (
                self.subTest(mutation=mutation),
                self.assertRaises((DataError, ValueError)),
            ):
                evaluate(data, self.db)
            self.assertFalse(self.db.exists())

    def test_interruption_at_segment_boundary_resumes_without_duplicate_costs(self):
        data = bundle(10, (5, 5))
        boundary = data["segments"][1]["interval_costs"][0]["at_ms"]
        apply = PaperLedger.apply

        def fault(book, event):
            disk = book._db.execute("PRAGMA database_list").fetchone()[2]
            if disk and event["at_ms"] == boundary:
                raise RuntimeError("INJECTED_BOUNDARY_CRASH")
            return apply(book, event)

        with (
            patch.object(PaperLedger, "apply", fault),
            self.assertRaisesRegex(RuntimeError, "INJECTED_BOUNDARY_CRASH"),
        ):
            evaluate(data, self.db)
        recovered = evaluate(data, self.db)
        expected = evaluate(data, Path(self.tmp.name) / "clean.sqlite")
        self.assertEqual(recovered, expected)
        self.assertEqual(
            Decimal(recovered["final_state"]["costs_usd"]), Decimal("0.010")
        )

    def test_modification_of_committed_segment_rejected_without_reset(self):
        data = bundle(10, (5, 5))
        before = evaluate(data, self.db)
        changed = copy.deepcopy(data)
        changed["segments"][0]["interval_costs"][1]["infrastructure_cost_usd"] = "0.002"
        with self.assertRaisesRegex(DataError, "SESSION_INPUT_CHANGED_OR_TRUNCATED"):
            evaluate(changed, self.db)
        self.assertEqual(evaluate(data, self.db), before)


if __name__ == "__main__":
    unittest.main()
