"""Bounded pagination for public Kalshi market discovery.

This module only reads public market metadata/order books.  It has no account,
authentication, order, balance, transfer, or execution functions.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Callable

from cycle_contract import (
    COVERAGE_SCHEMA,
    MAX_ORDERBOOKS,
    CycleContractError,
    validate_orderbook_limit,
)


class PaginationError(RuntimeError):
    """Provider data cannot be trusted enough to continue this cycle."""


def strict_m1_book(raw_orderbook_fp: object) -> dict | None:
    """Capture yes_dollars/no_dollars exactly as Kalshi returned them, for M1 only.

    Fails closed (returns None) on a missing side, a wrong row shape, or a
    non-string price/quantity, instead of dropping the bad row and silently
    certifying whatever remains as a complete book. `sanitize_levels`
    (`collector.numeric_levels`) makes that drop-and-stringify trade-off on
    purpose for general research dashboards; M1 review must never receive a
    partially invalid raw book disguised as a complete one, and must never
    see a value coerced away from the exact string Kalshi sent.
    """
    if not isinstance(raw_orderbook_fp, dict):
        return None
    sides: dict[str, list[list[str]]] = {}
    for key in ("yes_dollars", "no_dollars"):
        rows = raw_orderbook_fp.get(key)
        if not isinstance(rows, list):
            return None
        clean: list[list[str]] = []
        for row in rows:
            if type(row) is not list or len(row) != 2:
                return None
            price, qty = row
            if type(price) is not str or type(qty) is not str:
                return None
            clean.append([price, qty])
        sides[key] = clean
    return {"yes_bids": sides["yes_dollars"], "no_bids": sides["no_dollars"]}


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def collect_paginated(
    reader,
    *,
    origin: str,
    series: str,
    sanitize_levels: Callable[[object], dict],
    now: datetime | None = None,
    horizon_hours: int = 72,
    page_size: int = 100,
    max_pages: int = 50,
    max_orderbooks: int = MAX_ORDERBOOKS,
    orderbook_depth: int = 20,
) -> tuple[list[dict], dict]:
    """Discover every page up to explicit safety caps, then fetch near-term books.

    Coverage metadata is returned separately so a capped/partial scan cannot be
    represented as a complete sweep.
    """
    if not re.fullmatch(r"[A-Z0-9_-]{3,128}", series):
        raise PaginationError("invalid series ticker")
    if not (1 <= page_size <= 1000 and 1 <= max_pages <= 200):
        raise PaginationError("invalid pagination limits")
    try:
        validate_orderbook_limit(max_orderbooks)
    except CycleContractError as exc:
        raise PaginationError(str(exc)) from None
    if not (1 <= horizon_hours <= 24 * 30 and 1 <= orderbook_depth <= 100):
        raise PaginationError("invalid collection horizon")

    now = (now or datetime.now(UTC)).astimezone(UTC)
    horizon_end = now + timedelta(hours=horizon_hours)
    cursor: str | None = None
    pages = 0
    cursor_exhausted = False
    duplicate_tickers = 0
    invalid_close_time = 0
    by_ticker: dict[str, dict] = {}

    while pages < max_pages:
        params: dict[str, str | int] = {
            "series_ticker": series,
            "status": "open",
            "limit": page_size,
        }
        if cursor:
            params["cursor"] = cursor
        body = reader.get_json(origin, "/markets", params)
        if not isinstance(body, dict) or not isinstance(body.get("markets"), list):
            raise PaginationError("unexpected Kalshi markets response")
        pages += 1

        for market in body["markets"]:
            if not isinstance(market, dict):
                continue
            ticker = market.get("ticker")
            if not isinstance(ticker, str) or not re.fullmatch(r"[A-Z0-9-]{3,128}", ticker):
                continue
            if ticker in by_ticker:
                duplicate_tickers += 1
                continue
            by_ticker[ticker] = market

        next_cursor = body.get("cursor")
        if next_cursor in (None, ""):
            cursor_exhausted = True
            cursor = None
            break
        if not isinstance(next_cursor, str):
            raise PaginationError("invalid Kalshi cursor")
        if next_cursor == cursor:
            raise PaginationError("repeated Kalshi cursor")
        cursor = next_cursor

    eligible: list[tuple[datetime, str, dict]] = []
    for ticker, market in by_ticker.items():
        close_at = _parse_utc(market.get("close_time"))
        if close_at is None:
            invalid_close_time += 1
            continue
        if now <= close_at <= horizon_end:
            eligible.append((close_at, ticker, market))
    eligible.sort(key=lambda row: (row[0], row[1]))

    truncated_by_orderbook_limit = len(eligible) > max_orderbooks
    selected = eligible[:max_orderbooks]
    result: list[dict] = []
    for close_at, ticker, market in selected:
        ob = reader.get_json(origin, f"/markets/{ticker}/orderbook", {"depth": orderbook_depth})
        # Own clock read at the instant this book's fetch returned — never
        # coverage.generated_at/packet.generated_at (cycle-start) nor a later
        # report time, so a caller cannot mistake a stale book for a fresh one.
        book_observed_at = datetime.now(UTC)
        if not isinstance(ob, dict):
            raise PaginationError("unexpected Kalshi orderbook response")
        raw_fp = ob.get("orderbook_fp")
        book = ob.get("orderbook_fp", ob.get("orderbook", {}))
        result.append({
            "ticker": ticker,
            "event_ticker": market.get("event_ticker") if isinstance(market.get("event_ticker"), str) else None,
            "status": market.get("status") if isinstance(market.get("status"), str) else None,
            # The outcome name M2's matcher crosses against the sportsbook feed (M5 fair).
            "yes_sub_title": (
                market["yes_sub_title"][:100]
                if isinstance(market.get("yes_sub_title"), str) else None
            ),
            "close_time": close_at.isoformat(),
            "book_observed_at": book_observed_at.isoformat(),
            "levels": sanitize_levels(book),
            "m1_book": strict_m1_book(raw_fp),
        })

    coverage = {
        "schema_version": COVERAGE_SCHEMA,
        "generated_at": now.isoformat(),
        "series": series,
        "horizon_hours": horizon_hours,
        "pages_fetched": pages,
        "cursor_exhausted": cursor_exhausted,
        "truncated_by_page_limit": not cursor_exhausted and cursor is not None,
        "open_markets_seen": len(by_ticker),
        "duplicate_tickers": duplicate_tickers,
        "invalid_close_time": invalid_close_time,
        "eligible_in_horizon": len(eligible),
        "orderbooks_fetched": len(result),
        "truncated_by_orderbook_limit": truncated_by_orderbook_limit,
    }
    return result, coverage
