"""
Tests del poller shadow del Motor 2 (poller.py).

Verifica: un ciclo cruza Kalshi↔odds y emite señales; con fuente FAKE NO persiste
(edges no reales); con fuente LIVE persiste EdgeWindow(kind="consensus"); el path JAMÁS
ejecuta capital; el loop respeta stop_event.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlmodel import select

import src.storage.models as models
from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import KalshiEventQuotes, KalshiQuote
from src.strategies.motor_2_consensus.poller import Motor2ShadowPoller

EV = "KXWCGAME-26JUN27JORARG"


class _FakeKalshiSource:
    def __init__(self, events: list[KalshiEventQuotes]):
        self._events = events

    async def fetch(self) -> list[KalshiEventQuotes]:
        return list(self._events)


class _StubOdds:
    def __init__(self, events: list[OddsEvent], *, is_live: bool):
        self._events = events
        self.is_live = is_live

    async def fetch(self) -> list[OddsEvent]:
        return list(self._events)


def _kalshi_event() -> KalshiEventQuotes:
    # Argentina barata en Kalshi (40c YES) → con fair alto, edge YES grande.
    return KalshiEventQuotes(
        event_key=EV,
        outcomes=(
            KalshiQuote(f"{EV}-ARG", "Argentina", yes_ask_cents=40, no_ask_cents=62),
            KalshiQuote(f"{EV}-JOR", "Jordan", yes_ask_cents=8, no_ask_cents=93),
            KalshiQuote(f"{EV}-TIE", "Draw", yes_ask_cents=30, no_ask_cents=71),
        ),
    )


def _odds_event() -> OddsEvent:
    # Consenso: Argentina favorita fuerte → fair_prob ~0.7 ≫ ask 40c.
    from datetime import UTC, datetime

    outcomes = (
        Outcome("Argentina", 1.30),
        Outcome("Jordan", 11.0),
        Outcome("Draw", 5.0),
    )
    return OddsEvent(
        id="x",
        sport_key="soccer_fifa_world_cup",
        commence_time=datetime(2026, 6, 27, tzinfo=UTC),
        home_team="Argentina",
        away_team="Jordan",
        bookmakers=(Bookmaker("book", "Book", (Market("h2h", outcomes),)),),
    )


def _consensus_windows() -> list[models.EdgeWindow]:
    with models.get_session() as s:
        rows = list(s.exec(select(models.EdgeWindow)).all())
    return [w for w in rows if w.kind == "consensus"]


@pytest.mark.asyncio
async def test_poll_once_emits_signals_for_matched_event():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=False),
        capital_usd=300.0,
    )
    signals = await poller.poll_once()
    assert any(s.market_ticker == f"{EV}-ARG" and s.kalshi_side == "YES" for s in signals)


@pytest.mark.asyncio
async def test_fake_source_does_not_persist():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=False),  # FAKE → no persiste
        capital_usd=300.0,
    )
    signals = await poller.poll_once()
    assert signals  # hubo señales...
    assert _consensus_windows() == []  # ...pero NADA en la DB (fuente no-live)


@pytest.mark.asyncio
async def test_live_source_persists_consensus_windows():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=True),  # LIVE → persiste
        capital_usd=300.0,
    )
    signals = await poller.poll_once()
    wins = _consensus_windows()
    assert len(wins) == len(signals) > 0
    assert all(w.kind == "consensus" and w.edge_pct > 0 for w in wins)


@pytest.mark.asyncio
async def test_poll_once_no_kalshi_returns_empty():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([]),
        _StubOdds([_odds_event()], is_live=True),
        capital_usd=300.0,
    )
    assert await poller.poll_once() == []
    assert _consensus_windows() == []  # sin quotes Kalshi → ni siquiera consulta odds


@pytest.mark.asyncio
async def test_run_loop_stops_on_event():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=False),
        interval_sec=0.01,
        capital_usd=300.0,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(poller.run(stop), _stop_soon())
    assert stop.is_set()
