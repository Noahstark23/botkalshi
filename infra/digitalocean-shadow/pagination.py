"""Bounded pagination for public Kalshi market discovery.

This module only reads public market metadata/order books.  It has no account,
authentication, order, balance, transfer, or execution functions.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Callable


class PaginationError(RuntimeError):
    """Provider data cannot be trusted enough to continue this cycle."""


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
    max_orderbooks: int = 200,
    orderbook_depth: int = 20,
) -> tuple[list[dict], dict]:
    """Discover every page up to explicit safety caps, then fetch near-term books.

    Coverage metadata is returned separately so a capped/partial scan cannot be
    represented as a complete sweep.
    """
    if not re.fullmatch(r"[A-Z0-9_-]{3,128}", series):
        raise PaginationError("invalid series ticker")
    if not (1 <= page_size <= 1000 and 1 <= max_pages <= 200 and 1 <= max_orderbooks <= 2000):
        raise PaginationError("invalid pagination limits")
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
        if not isinstance(ob, dict):
            raise PaginationError("unexpected Kalshi orderbook response")
        book = ob.get("orderbook_fp", ob.get("orderbook", {}))
        result.append({
            "ticker": ticker,
            "event_ticker": market.get("event_ticker") if isinstance(market.get("event_ticker"), str) else None,
            "status": market.get("status") if isinstance(market.get("status"), str) else None,
            "close_time": close_at.isoformat(),
            "levels": sanitize_levels(book),
        })

    coverage = {
        "schema_version": "botkalshi-coverage-v1",
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
