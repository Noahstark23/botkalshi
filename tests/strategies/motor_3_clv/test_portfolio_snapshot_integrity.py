"""La cartera cacheada nunca se reemplaza por un prefijo de páginas."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from src.storage.models import PortfolioPosition, get_session
from src.strategies.motor_3_clv.poller import PortfolioPoller


def client_for(pages):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.get_positions.side_effect = pages
    client.get_market.return_value = {"market": {"close_time": "2030-01-01T00:00:00Z"}}
    return client


def snapshot():
    with get_session() as session:
        return {p.ticker: p.model_dump() for p in session.exec(select(PortfolioPosition))}


def seed():
    with get_session() as session:
        for ticker in ("A", "B"):
            session.add(
                PortfolioPosition(
                    ticker=ticker,
                    side="yes",
                    count=3,
                    peak_bid_cents=88,
                    synced_at=datetime(2026, 1, 1),
                )
            )
        session.commit()


def page(ticker="A", cursor=""):
    return {"market_positions": [{"ticker": ticker, "position_fp": "0.25"}], "cursor": cursor}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"market_positions": [], "cursor": None},
        {"market_positions": [], "cursor": 0},
        {"market_positions": [], "cursor": False},
        {"market_positions": [], "cursor": " "},
        {"market_positions": [], "cursor": "x" * 4097},
        {"market_positions": None, "cursor": ""},
        {"market_positions": [None], "cursor": ""},
        {"market_positions": [{"ticker": None, "position": 1}], "cursor": ""},
        {"market_positions": [{"ticker": 12, "position": 1}], "cursor": ""},
        {"market_positions": [{"ticker": " ", "position": 1}], "cursor": ""},
        {"market_positions": [], "positions": [{"ticker": "B", "position": 1}], "cursor": ""},
        page("A"),
        page("C", "next"),
        page("C", "more"),
        {
            "market_positions": [{"ticker": f"C{i}", "position": 1} for i in range(101)],
            "cursor": "",
        },
        TimeoutError("simulated page timeout"),
    ],
)
async def test_incomplete_or_ambiguous_pages_preserve_exact_cache(monkeypatch, bad):
    seed()
    before = snapshot()
    client = client_for([page("A", "next"), bad])
    poller = PortfolioPoller(client_factory=lambda: client)
    poller._empty_syncs = 1
    monkeypatch.setattr(poller, "_MAX_PAGES", 2)
    monkeypatch.setattr(poller, "_GET_MARKET_PAUSE_SEC", 0)
    failure = None
    try:
        await poller.sync_once()
    except (ValueError, TimeoutError) as exc:
        failure = exc
    assert snapshot() == before
    assert failure is not None
    assert poller._empty_syncs == 0
    client.get_market.assert_not_awaited()
    assert client.get_positions.await_count == 2


async def test_complete_two_pages_keep_fractional_positions(monkeypatch):
    seed()
    client = client_for([page("A", "next"), page("B")])
    poller = PortfolioPoller(client_factory=lambda: client)
    monkeypatch.setattr(poller, "_GET_MARKET_PAUSE_SEC", 0)
    assert await poller.sync_once() == 2
    rows = snapshot()
    assert set(rows) == {"A", "B"}
    assert all(row["count_fp"] == "0.25" and row["peak_bid_cents"] == 88 for row in rows.values())


async def test_failure_between_empty_snapshots_breaks_purge_confirmation():
    seed()
    before = snapshot()
    empty = {"market_positions": [], "cursor": ""}
    client = client_for([empty, {"cursor": ""}, empty, empty])
    poller = PortfolioPoller(client_factory=lambda: client)
    assert await poller.sync_once() == 0
    with pytest.raises(ValueError):
        await poller.sync_once()
    assert snapshot() == before
    assert await poller.sync_once() == 0
    assert snapshot() == before
    assert await poller.sync_once() == 0
    assert snapshot() == {}


async def test_empty_intermediate_page_is_not_terminal(monkeypatch):
    seed()
    client = client_for([{"positions": [], "cursor": "next"}, page("B")])
    poller = PortfolioPoller(client_factory=lambda: client)
    monkeypatch.setattr(poller, "_GET_MARKET_PAUSE_SEC", 0)
    assert await poller.sync_once() == 1
    assert set(snapshot()) == {"B"}
    assert client.get_positions.await_count == 2


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancellation_preserves_cache_and_breaks_empty_sequence(
    monkeypatch, cancel
):
    seed()
    before = snapshot()
    client = client_for([])
    entered = asyncio.Event()

    async def blocked(**_):
        entered.set()
        await asyncio.Event().wait()

    client.get_positions.side_effect = blocked
    poller = PortfolioPoller(client_factory=lambda: client)
    poller._empty_syncs = 1
    if cancel:
        task = asyncio.create_task(poller.sync_once())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        monkeypatch.setattr(poller, "_FETCH_TIMEOUT_SEC", 0.01)
        with pytest.raises(TimeoutError):
            await poller.sync_once()
    assert snapshot() == before
    assert poller._empty_syncs == 0
    client.get_market.assert_not_awaited()
