"""Create the FICTIONAL simulation bank for the research service — an explicit operator act.

Usage (on the research droplet, as the service user):

    python3 init_sim_bank.py --data /var/lib/botkalshi-research --initial-capital-usd 200.00

The runner never creates capital: without this step M5 research reports BLOCKED_NO_BANK.
Idempotent for the SAME amount (prints the current snapshot, changes nothing); a
DIFFERENT amount is refused (SimulationBankConflictError) — capital is never redefined
silently and there is no reset. The capital is fictional: no account, no money, no key.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import simulation_bank as bank

BANK_FILE = "simulation-bank.sqlite3"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--initial-capital-usd", required=True)
    args = parser.parse_args(argv)
    try:
        snap = bank.init_bank(args.data / BANK_FILE, initial_capital_usd=args.initial_capital_usd)
    except bank.SimulationBankError as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    keys = (
        "mode",
        "capital_source",
        "initial_capital_usd",
        "capital_usd",
        "reserved_usd",
        "execution_authorized",
        "revision",
    )
    print(json.dumps({k: snap[k] for k in keys}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
