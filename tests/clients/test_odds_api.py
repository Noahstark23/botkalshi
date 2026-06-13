"""
Tests del OddsAPIClient — parsing tipado, auth por apiKey, backoff y record_error.

Usa httpx.MockTransport (incluido en httpx, sin dependencia nueva) para ejercitar el
_request REAL (apiKey en la query, retries, clasificación de errores) sin red.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from src.clients.odds_api import (
    OddsAPIAuthError,
    OddsAPIClient,
    OddsAPIRateLimitError,
)

# Shape REAL de The Odds API v4 (un evento de fútbol 3-way con Pinnacle).
_ODDS_FIXTURE = [
    {
        "id": "abc123",
        "sport_key": "soccer_fifa_world_cup",
        "sport_title": "FIFA World Cup",
        "commence_time": "2026-06-12T18:00:00Z",
        "home_team": "Argentina",
        "away_team": "Mexico",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Argentina", "price": 1.80},
                            {"name": "Mexico", "price": 4.50},
                            {"name": "Draw", "price": 3.60},
                        ],
                    }
                ],
            }
        ],
    }
]


def _client(handler) -> OddsAPIClient:
    c = OddsAPIClient(api_key="TESTKEY", transport=httpx.MockTransport(handler))
    c.RETRY_WAIT_MIN = 0.0  # sin sleep real en los tests de retry
    c.RETRY_WAIT_MAX = 0.0
    return c


@pytest.mark.asyncio
async def test_get_odds_parses_typed_models_and_sends_api_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_ODDS_FIXTURE)

    async with _client(handler) as client:
        events = await client.get_odds("soccer_fifa_world_cup")

    # apiKey + params en la query.
    assert "apiKey=TESTKEY" in captured["url"]
    assert "markets=h2h" in captured["url"] and "oddsFormat=decimal" in captured["url"]
    # Parseo tipado correcto.
    assert len(events) == 1
    ev = events[0]
    assert ev.id == "abc123" and ev.home_team == "Argentina" and ev.away_team == "Mexico"
    assert ev.commence_time.year == 2026 and ev.commence_time.tzinfo is not None  # aware UTC
    h2h = ev.bookmakers[0].markets[0]
    assert h2h.key == "h2h" and len(h2h.outcomes) == 3  # 3-way (incluye Draw)
    draw = next(o for o in h2h.outcomes if o.name == "Draw")
    assert draw.price == 3.60


@pytest.mark.asyncio
async def test_malformed_event_is_skipped_not_crashing():
    """Un evento sin campos requeridos se descarta; los válidos se devuelven."""
    payload = [{"id": "broken"}, _ODDS_FIXTURE[0]]  # primero malformado

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _client(handler) as client:
        events = await client.get_odds("soccer_fifa_world_cup")

    assert len(events) == 1 and events[0].id == "abc123"


@pytest.mark.asyncio
async def test_429_then_200_retries_and_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=_ODDS_FIXTURE)

    async with _client(handler) as client:
        events = await client.get_odds("soccer_fifa_world_cup")

    assert calls["n"] == 2  # reintentó tras el 429
    assert len(events) == 1


@pytest.mark.asyncio
async def test_persistent_429_records_error_and_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    with patch("src.monitoring.health.BotState.record_error") as mock_rec:
        async with _client(handler) as client:
            with pytest.raises(OddsAPIRateLimitError):
                await client.get_odds("soccer_fifa_world_cup")

    mock_rec.assert_called()  # error de red registrado para /status
    assert "OddsAPI" in mock_rec.call_args.args[0]


@pytest.mark.asyncio
async def test_network_error_records_and_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with patch("src.monitoring.health.BotState.record_error") as mock_rec:
        async with _client(handler) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get_odds("soccer_fifa_world_cup")
    mock_rec.assert_called()


@pytest.mark.asyncio
async def test_missing_api_key_raises_auth_error():
    client = OddsAPIClient(
        api_key="", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    )
    async with client:
        with pytest.raises(OddsAPIAuthError):
            await client.get_odds("soccer_fifa_world_cup")


@pytest.mark.asyncio
async def test_get_sports_returns_list():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "apiKey=TESTKEY" in str(request.url)
        return httpx.Response(
            200, json=[{"key": "soccer_fifa_world_cup", "title": "FIFA World Cup"}]
        )

    async with _client(handler) as client:
        sports = await client.get_sports()
    assert sports[0]["key"] == "soccer_fifa_world_cup"
