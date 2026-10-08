"""Bounded, read-only liquidation marks shared by M1/M2/M5.

No account keys, odds requests or order methods are used here. Missing book or
fee evidence invalidates both sides; a failed inventory read stops the required
service after clearing its marks. No price is inferred from the consensus fair.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from sqlmodel import col, select

from src.risk.manager import RiskManager
from src.storage.models import Trade, get_session
from src.strategies.data_capture import parse_price_to_cents, rest_orderbook_sides
from src.strategies.motor_5_mm.fee_policy import SeriesFeePolicy

MAX_TICKERS = 8


def open_tickers() -> list[str]:
    with get_session() as session:
        return list(
            session.exec(
                select(Trade.ticker)
                .where(col(Trade.status).in_(["pending", "filled"]))
                .distinct()
                .limit(MAX_TICKERS + 1)
            )
        )


def _bid(levels: list) -> int:
    prices = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise ValueError("invalid book level")
        if isinstance(level[1], bool):
            raise ValueError("invalid depth")
        try:
            depth = Decimal(str(level[1]))
        except (InvalidOperation, ValueError):
            raise ValueError("invalid depth") from None
        price = parse_price_to_cents(level[0])
        if not depth.is_finite() or depth < 0 or type(price) is not int or not 1 <= price <= 99:
            raise ValueError("invalid book level")
        if depth > 0:
            prices.append(price)
    if not prices:
        raise ValueError("missing executable side")
    return max(prices)


class PositionMarkRefresher:
    def __init__(self, client, *, ticker_provider=open_tickers, fee_policy=None, clock=None):
        self._client = client
        self._tickers = ticker_provider
        self._fees = fee_policy if fee_policy is not None else SeriesFeePolicy(client)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._observed: set[str] = set()

    @staticmethod
    def _invalidate(ticker: str) -> None:
        for side in ("yes", "no"):
            RiskManager.record_mark(ticker, side, None)

    async def refresh(self) -> dict:
        try:
            tickers = sorted(set(self._tickers()))
            if len(tickers) > MAX_TICKERS or any(
                not isinstance(t, str) or not t.startswith("KXMLBGAME-") or len(t) > 128
                for t in tickers
            ):
                raise ValueError("multi-engine inventory outside bounded profile")
        except Exception:
            # Existing marks from another publisher must not mask lost coverage.
            RiskManager._marks.clear()
            raise RuntimeError("position mark inventory unavailable") from None
        for ticker in self._observed - set(tickers):
            self._invalidate(ticker)
        self._observed = set(tickers)
        result = {"observed": [], "unavailable": []}
        for ticker in tickers:
            try:
                await asyncio.wait_for(self._refresh_one(ticker), timeout=5)
            except Exception:
                self._invalidate(ticker)
                result["unavailable"].append(ticker)
            else:
                result["observed"].append(ticker)
        return result

    async def _refresh_one(self, ticker: str) -> None:
        body = await self._client.get_market(ticker)
        market = body.get("market") if isinstance(body, dict) else None
        if not isinstance(market, dict) or market.get("ticker") != ticker:
            raise ValueError("market identity unavailable")
        event = market.get("event_ticker")
        if not isinstance(event, str) or not event.startswith("KXMLBGAME-"):
            raise ValueError("event identity unavailable")
        book = await self._client.get_orderbook(ticker)
        observed_at = self._clock()
        sides = rest_orderbook_sides(book)
        yes_bid, no_bid = _bid(sides["yes"]), _bid(sides["no"])
        if yes_bid + no_bid > 100:
            raise ValueError("crossed book")
        fee = await self._fees.observe(ticker, event_ticker=event)
        now = self._clock()
        if (
            observed_at.tzinfo is None
            or fee.observed_at.tzinfo is None
            or not 0 <= (now - observed_at).total_seconds() <= 30
            or not 0 <= (now - fee.observed_at).total_seconds() <= 30
            or fee.event_ticker != event
            or fee.series_ticker != "KXMLBGAME"
        ):
            raise ValueError("stale or mismatched evidence")
        RiskManager.record_mark(
            ticker,
            "yes",
            yes_bid,
            ask_cents=100 - no_bid,
            observed_at=observed_at,
            fee_multiplier=fee.multiplier,
        )
        RiskManager.record_mark(
            ticker,
            "no",
            no_bid,
            ask_cents=100 - yes_bid,
            observed_at=observed_at,
            fee_multiplier=fee.multiplier,
        )
