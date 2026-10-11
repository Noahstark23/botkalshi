"""Execute a bounded offline end-to-end paper session and verify restart."""

import argparse
import copy
import json
from pathlib import Path

from example_session import session
from us500_engine import save_new
from us500_runner import replay


def run(directory):
    directory = Path(directory)
    directory.mkdir(exist_ok=False)
    data = session()
    save_new(directory / "SESSION_SYNTHETIC.json", data)
    prefix = copy.deepcopy(data)
    prefix["frames"] = prefix["frames"][:4]
    save_new(
        directory / "PREFIX_REPORT.json", replay(prefix, directory / "paper.sqlite3")
    )
    final = replay(data, directory / "paper.sqlite3")
    restarted = replay(data, directory / "paper.sqlite3")
    if final != restarted:
        raise RuntimeError("RESTART_DIVERGENCE")
    save_new(directory / "FINAL_REPORT.json", final)
    summary = {
        "synthetic": True,
        "restart_equal": True,
        "initial_cash_usd": data["ledger"]["initial_cash_usd"],
        "final_cash_usd": final["final_state"]["cash_usd"],
        "net_pnl_usd": final["final_state"]["net_pnl_usd"],
        "fees_usd": final["final_state"]["fees_usd"],
        "funding_usd": final["final_state"]["funding_usd"],
        "costs_usd": final["final_state"]["costs_usd"],
        "residual_position_quantity": final["residual_position_quantity"],
        "reserved_margin_usd": final["final_state"]["reserved_margin_usd"],
        "events": final["ledger_events"],
        "actual_orders": 0,
        "strategy_validated": False,
        "execution_available": False,
    }
    save_new(directory / "SUMMARY.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().directory), indent=2))
