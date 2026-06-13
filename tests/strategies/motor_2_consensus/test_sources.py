"""
Tests de las fuentes del cable shadow del Motor 2 (sources.py).

Verifica: el extractor de Kalshi parsea nombre real (yes_sub_title) + ambos asks y
descarta eventos incompletos; FakeOddsSource es no-live y devuelve el fixture;
LiveOddsSource concatena sport_keys y tolera un deporte que falla.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_2_consensus.fixtures import world_cup_demo_fixture
from src.strategies.motor_2_consensus.sources import (
    FakeOddsSource,
    LiveOddsSource,
    RestKalshiQuoteSource,
    _parse_event_quotes,
)

EV = "KXWCGAME-26JUN27JORARG"


def _market(ticker: str, name: str, yes_ask: str, no_ask: str) -> dict:
    return {
        "ticker": ticker,
        "yes_sub_title": name,
        "yes_ask_dollars": yes_ask,
        "no_ask_dollars": no_ask,
        "status": "open",
    }


def _fake_client_factory(events_by_key: dict[str, dict]):
    """client_factory que devuelve un KalshiRestClient falso (async ctx mgr) con get_event."""

    class _FakeClient:
        async def get_event(self, event_key: str) -> dict:
            return events_by_key.get(event_key, {"markets": []})

    @asynccontextmanager
    async def _factory_cm():
        yield _FakeClient()

    # RestKalshiQuoteSource hace `async with self._client_factory() as client`
    return lambda: _factory_cm()


def test_parse_event_quotes_extracts_real_names_and_both_asks():
    markets = [
        _market(f"{EV}-ARG", "Argentina", "0.55", "0.46"),
        _market(f"{EV}-JOR", "Jordan", "0.08", "0.93"),
        _market(f"{EV}-TIE", "Draw", "0.30", "0.71"),
    ]
    eq = _parse_event_quotes(EV, markets)
    assert eq is not None
    assert eq.event_key == EV
    names = {q.outcome_name for q in eq.outcomes}
    assert names == {"Argentina", "Jordan", "Draw"}
    arg = next(q for q in eq.outcomes if q.outcome_name == "Argentina")
    assert arg.yes_ask_cents == 55 and arg.no_ask_cents == 46


def test_parse_event_quotes_skips_markets_missing_data():
    markets = [
        _market(f"{EV}-ARG", "Argentina", "0.55", "0.46"),
        {"ticker": f"{EV}-JOR", "yes_sub_title": "", "yes_ask_dollars": "0.08"},  # sin nombre
    ]
    # Solo 1 outcome usable → evento descartado (<2).
    assert _parse_event_quotes(EV, markets) is None


@pytest.mark.asyncio
async def test_rest_kalshi_source_builds_event_quotes():
    events_by_key = {
        EV: {
            "markets": [
                _market(f"{EV}-ARG", "Argentina", "0.55", "0.46"),
                _market(f"{EV}-JOR", "Jordan", "0.08", "0.93"),
                _market(f"{EV}-TIE", "Draw", "0.30", "0.71"),
            ]
        }
    }
    src = RestKalshiQuoteSource(
        lambda: {EV: {f"{EV}-ARG", f"{EV}-JOR", f"{EV}-TIE"}},
        client_factory=_fake_client_factory(events_by_key),
        pause_sec=0.0,
    )
    out = await src.fetch()
    assert len(out) == 1
    assert out[0].event_key == EV
    assert len(out[0].outcomes) == 3


@pytest.mark.asyncio
async def test_rest_kalshi_source_empty_universe_returns_empty():
    src = RestKalshiQuoteSource(lambda: {}, client_factory=_fake_client_factory({}))
    assert await src.fetch() == []


@pytest.mark.asyncio
async def test_fake_odds_source_is_not_live_and_returns_fixture():
    fixture = world_cup_demo_fixture()
    fake = FakeOddsSource(fixture)
    assert fake.is_live is False
    out = await fake.fetch()
    assert len(out) == len(fixture) == 2
    # Los nombres de outcome del fixture canonizan a selección + draw (cruzables con Kalshi).
    names = {o.name for ev in out for bk in ev.bookmakers for mk in bk.markets for o in mk.outcomes}
    assert "Argentina" in names and "Draw" in names


@pytest.mark.asyncio
async def test_live_odds_source_concatenates_and_tolerates_failure():
    fixture = world_cup_demo_fixture()

    class _FakeOddsClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        get_odds = AsyncMock(side_effect=[fixture, RuntimeError("rate limit")])

    live = LiveOddsSource(["soccer_a", "soccer_b"], client_factory=_FakeOddsClient)
    assert live.is_live is True
    out = await live.fetch()
    # soccer_a devolvió el fixture; soccer_b falló pero no tiró el batch.
    assert len(out) == len(fixture)
