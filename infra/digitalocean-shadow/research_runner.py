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
from m1_observation import SCHEMA_VERSION_INPUT, review_m1_book
from pagination import PaginationError, collect_paginated
from risk_guard import write_status


M1_CYCLE_REVIEW_SCHEMA = "botkalshi-m1-cycle-review-v1"


_ORIGINAL_CYCLE = collector.cycle


def _collection_now() -> datetime:
    """Own indirection point so tests can pin the collection clock deterministically."""
    return datetime.now(UTC)


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
            now=_collection_now(),
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


def _m1_book_input(cycle_id: str, market: object) -> dict:
    """Adapt one collected market into review_m1_book's closed input schema.

    Sourced ONLY from pagination's strict `m1_book` capture — never from the
    general-purpose `levels` field. `levels` is built by
    `collector.numeric_levels`, which silently drops malformed rows and
    stringifies numbers; that normalizer is fine for research dashboards but
    would let a partially invalid raw fixed-point book pass through to M1
    review looking complete. `m1_book` instead fails closed (None) on any
    missing side or malformed/non-string row, which becomes an explicit
    empty side here — review_m1_book fails closed on that (NO_DATA); it is
    never treated as zero and never a cleaned-up subset of a bad book.
    """
    if not isinstance(market, dict):
        market = {}
    strict = market.get("m1_book")
    if not isinstance(strict, dict):
        strict = {}
    yes_bids = strict.get("yes_bids")
    no_bids = strict.get("no_bids")
    return {
        "schema_version": SCHEMA_VERSION_INPUT,
        "cycle_id": cycle_id,
        "ticker": market.get("ticker"),
        "book_observed_at": market.get("book_observed_at"),
        "price_unit": "USD",
        "yes_bids": yes_bids if isinstance(yes_bids, list) else [],
        "no_bids": no_bids if isinstance(no_bids, list) else [],
    }


def _review_m1_cycle(cycle_id: str, packet: dict) -> dict:
    kalshi = packet.get("kalshi")
    markets = kalshi.get("markets") if isinstance(kalshi, dict) else None
    if not isinstance(markets, list):
        markets = []
    reviews = [review_m1_book(_m1_book_input(cycle_id, market)) for market in markets]
    return {
        "schema_version": M1_CYCLE_REVIEW_SCHEMA,
        "cycle_id": cycle_id,
        "generated_at": collector.utc_now(),
        "market_count": len(reviews),
        "reviews": reviews,
    }


def _cycle_with_risk(reader, con, odds_cache):
    result = _ORIGINAL_CYCLE(reader, con, odds_cache)
    packet_path = collector.DATA_DIR / "packets" / "latest.json"
    packet = None
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
    # M1 review is a separate diagnostic artifact keyed by cycle_id; it never
    # writes into packet.assessment and a failure here never blocks the cycle.
    if cycle_id is not None and isinstance(packet, dict):
        try:
            m1_review = _review_m1_cycle(cycle_id, packet)
            collector.atomic_write(collector.DATA_DIR / "m1" / "latest.json", m1_review)
        except (OSError, UnicodeError, TypeError, ValueError):
            pass
    return result


def main() -> int:
    # Patch only the two extension points.  The original collector keeps its
    # safety_check, durable SQLite writes, health file, signal handling and loop.
    collector.collect_kalshi = _collect_kalshi
    collector.cycle = _cycle_with_risk
    return collector.main()


if __name__ == "__main__":
    raise SystemExit(main())
