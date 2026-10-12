"""Synthetic benchmark: PYTHONPATH=.:tests python3 evidence/V2_7_1/benchmark_long.py 7141.

Run from research/us500; no captured data or real account parameters.
"""

import json
import resource
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from test_us500_long_evaluation import bundle

from us500_engine import body_digest
from us500_long_evaluation import evaluate

resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
n = int(sys.argv[1])
start = time.monotonic()
data = bundle(n, (n,))
data["ledger"].update(initial_cash_usd="1000000", max_loss_usd="1000000", fee_bps="0")
data["strategy"]["slippage_bps"] = "0"
for i, snap in enumerate(data["segments"][0]["snapshots"]):
    p = Decimal("13") + Decimal(i % 4) / 10
    market = snap["observations"]["market"]
    book = snap["observations"]["orderbook"]
    for field in ("reference_price", "liquidation_mark_price"):
        market["body"]["market"][field]["price"] = str(p)
    book["body"]["orderbook"] = {
        "bids": [[str(p - Decimal(".01")), "10"]],
        "asks": [[str(p + Decimal(".01")), "10"]],
    }
    for obs in (market, book):
        obs["canonical_body_sha256"] = body_digest(obs["body"])
for cost in data["segments"][0]["interval_costs"]:
    cost["funding_cashflow_usd"] = "0"
    cost["infrastructure_cost_usd"] = "0"
with (
    tempfile.TemporaryDirectory(prefix="us500-benchmark-") as tmp,
    patch("socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN")),
):
    db = Path(tmp) / "paper.sqlite"
    r = evaluate(data, db)
    print(
        json.dumps(
            {
                "synthetic": True,
                "frames": n,
                "elapsed_seconds": time.monotonic() - start,
                "maxrss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
                "db_bytes": db.stat().st_size,
                "ledger_events": r["ledger_events"],
                "orders": len(r["final_state"].get("orders", {})),
                "fill_frames": sum(
                    x["status"] == "HYPOTHETICAL_FILL" for x in r["frames"]
                ),
                "actual_orders": r["actual_orders"],
            }
        ),
        flush=True,
    )
