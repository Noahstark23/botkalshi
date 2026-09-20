"""Runtime composition for the read-only research service.

It extends the existing collector with bounded pagination/coverage evidence and a
local risk-policy status.  It deliberately does not add Kalshi authentication or
order capabilities.
"""
from __future__ import annotations

from datetime import UTC, datetime
import json
import os

import collector
from cycle_contract import MAX_ORDERBOOKS
from pagination import PaginationError, collect_paginated
from risk_guard import write_status


_ORIGINAL_CYCLE = collector.cycle


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise collector.ResearchError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise collector.ResearchError(f"{name} outside safe range")
    return value


def _collect_kalshi(reader) -> list[dict]:
    horizon_hours = _int_env("BOTKALSHI_HORIZON_HOURS", 72, 1, 24 * 30)
    page_size = _int_env("BOTKALSHI_PAGE_SIZE", 100, 1, 1000)
    max_pages = _int_env("BOTKALSHI_MAX_PAGES", 50, 1, 200)
    max_orderbooks = _int_env(
        "BOTKALSHI_MAX_ORDERBOOKS", MAX_ORDERBOOKS, 1, MAX_ORDERBOOKS
    )
    try:
        markets, coverage = collect_paginated(
            reader,
            origin=collector.KALSHI_ORIGIN,
            series=collector.SERIES,
            sanitize_levels=collector.numeric_levels,
            now=datetime.now(UTC),
            horizon_hours=horizon_hours,
            page_size=page_size,
            max_pages=max_pages,
            max_orderbooks=max_orderbooks,
            orderbook_depth=20,
        )
    except PaginationError as exc:
        raise collector.ResearchError(str(exc)) from None
    collector.atomic_write(collector.DATA_DIR / "coverage.json", coverage)
    return markets


def _cycle_with_risk(reader, con, odds_cache):
    result = _ORIGINAL_CYCLE(reader, con, odds_cache)
    packet_path = collector.DATA_DIR / "packets" / "latest.json"
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        cycle_id = packet["packet_id"]
        generated_at = packet["generated_at"]
        coverage_path = collector.DATA_DIR / "coverage.json"
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        coverage["cycle_id"] = cycle_id
        collector.atomic_write(coverage_path, coverage)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        cycle_id = None
        generated_at = None
    # Missing/invalid reconciliation is represented fail-closed in risk-status.json.
    write_status(collector.DATA_DIR, cycle_id=cycle_id, generated_at=generated_at)
    return result


def main() -> int:
    # Patch only the two extension points.  The original collector keeps its
    # safety_check, durable SQLite writes, health file, signal handling and loop.
    collector.collect_kalshi = _collect_kalshi
    collector.cycle = _cycle_with_risk
    return collector.main()


if __name__ == "__main__":
    raise SystemExit(main())
