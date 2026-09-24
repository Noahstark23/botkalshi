"""Runtime composition for the read-only research service.

It extends the existing collector with bounded pagination/coverage evidence and a
local risk-policy status.  It deliberately does not add Kalshi authentication or
order capabilities.
"""
from __future__ import annotations

import contextlib
from datetime import UTC, datetime
import json
import os
from pathlib import Path

import collector
from cycle_contract import MAX_ORDERBOOKS
from m1_observation import SCHEMA_VERSION_INPUT, review_m1_book
import m5_inputs
import m5_research_sim
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
    m1_review = None
    if cycle_id is not None and isinstance(packet, dict):
        try:
            m1_review = _review_m1_cycle(cycle_id, packet)
            collector.atomic_write(collector.DATA_DIR / "m1" / "latest.json", m1_review)
        except (OSError, UnicodeError, TypeError, ValueError):
            pass
    _run_m5_research(packet, m1_review, reader=reader)
    return result


def _m5_research_enabled() -> bool:
    return os.environ.get("BOTKALSHI_M5_RESEARCH_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


M5_HISTORY_MAX_LINES = 2_880  # ~2 days of 60 s cycles; nothing grows without a cap


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _append_bounded(path: Path, record: dict, max_lines: int = M5_HISTORY_MAX_LINES) -> None:
    """Append one JSON line and keep only the newest `max_lines` (atomic rewrite)."""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()[-(max_lines - 1):]
    lines.append(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _run_m5_research(packet: object, m1_review: object, reader=None) -> None:
    """M5 research REST: a PASSENGER of this cycle's capture and M1 review — no fetch of
    its own. Off unless BOTKALSHI_M5_RESEARCH_ENABLED. Best-effort twice over: any
    failure is written to m5/latest.json and the collector cycle goes on (Lesson 7)."""
    if not _m5_research_enabled():
        return
    out = collector.DATA_DIR / "m5" / "latest.json"
    try:
        bank_db = Path(os.environ.get(
            "BOTKALSHI_SIM_BANK_PATH", str(collector.DATA_DIR / "simulation-bank.sqlite3")
        ))
        producer = None
        if reader is not None and _flag("BOTKALSHI_M5_INPUTS_ENABLED") and isinstance(packet, dict):
            # Fee from Kalshi's public API every cycle; fair only through the budgeted,
            # owner-authorized odds pilot (off unless its own flags and key file exist).
            producer = m5_inputs.produce(reader, data_dir=collector.DATA_DIR, packet=packet)
        inputs, problem = m5_research_sim.load_inputs(collector.DATA_DIR / "m5" / "inputs.json")
        report = m5_research_sim.run_cycle(
            bank_db=bank_db, packet=packet, m1_review=m1_review,
            inputs=inputs, inputs_problem=problem,
        )
        report["inputs_producer"] = producer
    except Exception as exc:  # noqa: BLE001 — recorded, never raised into the collector
        report = {
            "schema_version": m5_research_sim.SCHEMA_OUTPUT,
            "cohort": m5_research_sim.COHORT,
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "execution_authorized": False,
        }
    with contextlib.suppress(OSError, TypeError, ValueError):
        collector.atomic_write(out, report)
    with contextlib.suppress(OSError, TypeError, ValueError):
        _append_bounded(collector.DATA_DIR / "m5" / "cycles.jsonl", report)


def main() -> int:
    # Patch only the two extension points.  The original collector keeps its
    # safety_check, durable SQLite writes, health file, signal handling and loop.
    collector.collect_kalshi = _collect_kalshi
    collector.cycle = _cycle_with_risk
    return collector.main()


if __name__ == "__main__":
    raise SystemExit(main())
