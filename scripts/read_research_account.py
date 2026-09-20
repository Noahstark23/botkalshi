"""Create a sanitized, read-only Kalshi account snapshot.

This command is intentionally separate from the shadow collector.  It never
places or cancels orders and its output does not unlock real-money execution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "infra" / "digitalocean-shadow"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SHADOW))

from account_reader import AccountReadError, atomic_write, collect_account_snapshot  # noqa: E402


EXECUTION_NAMES = (
    "TRADING_ENABLED",
    "MOTOR_MM_EXECUTION_ENABLED",
    "MOTOR_1_EXECUTION_ENABLED",
    "MOTOR_2_EXECUTION_ENABLED",
    "MOTOR_2_ENTRY_EXECUTION_ENABLED",
    "MOTOR_3_EXECUTION_ENABLED",
    "MOTOR_REST_EXECUTION_ENABLED",
)


def _assert_readonly_environment() -> None:
    for name in EXECUTION_NAMES:
        if os.environ.get(name, "").strip().lower() not in ("", "0", "false", "off", "no"):
            raise AccountReadError(f"{name} must remain false")


async def _run(output: Path) -> dict:
    from src.clients.kalshi_rest import KalshiRestClient

    _assert_readonly_environment()
    async with KalshiRestClient() as client:
        snapshot = await collect_account_snapshot(client)
    atomic_write(output, snapshot)
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        snapshot = asyncio.run(_run(args.output))
    except (AccountReadError, OSError, ValueError, RuntimeError) as exc:
        print(f"account snapshot blocked: {type(exc).__name__}", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "schema_version": snapshot["schema_version"],
                "observed_at": snapshot["observed_at"],
                "positions_count": snapshot["positions_count"],
                "fills_count": snapshot["fills_count"],
                "reconciled": False,
                "execution_authorized": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
