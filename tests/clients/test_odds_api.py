"""
Tests del OddsAPIClient — parsing tipado, auth por apiKey, backoff y record_error.

Usa httpx.MockTransport (incluido en httpx, sin dependencia nueva) para ejercitar el
_request REAL (apiKey en la query, retries, clasificación de errores) sin red.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.clients.odds_api import (
    OddsAPIAuthError,
    OddsAPIClient,
    OddsAPIRateLimitError,
)


@pytest.fixture(autouse=True)
def _odds_settings_and_class_state():
    """Settings mockeados (get_settings real exige la key privada de Kalshi) + reset del
    estado de CLASE del cliente (caché/breaker compartidos entre instancias — sin esto,
    un test contamina al siguiente con respuestas cacheadas)."""
    OddsAPIClient._cache = {}
    OddsAPIClient._quota_exhausted_at = None
    s = MagicMock()
    s.ODDS_API_CACHE_TTL_SEC = 60.0
    s.ODDS_API_QUOTA_COOLDOWN_SEC = 3600.0
    s.ODDS_API_KEY = "TESTKEY"
    with patch("src.clients.odds_api.get_settings", return_value=s):
        yield s
    OddsAPIClient._cache = {}
    OddsAPIClient._quota_exhausted_at = None


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


# =====================================================
# Caché con TTL + breaker de cuota (incidente créditos 2026-07-19: 20k quemados en días)
# =====================================================


def _counting_handler(counter: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] = counter.get("n", 0) + 1
        return httpx.Response(200, json=_ODDS_FIXTURE)

    return handler


class _FrozenDatetime(datetime):
    """datetime congelado y avanzable para el breaker (now inyectable sin dormir)."""

    current = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return cls.current if tz else cls.current.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_cache_serves_within_ttl_single_http_call():
    """(a) MECANISMO del caché: dos get_odds del mismo (sport, región) dentro del TTL →
    UNA sola llamada HTTP (la segunda sale del caché de clase, cero créditos)."""
    counter: dict = {}
    async with _client(_counting_handler(counter)) as client:
        first = await client.get_odds("soccer_fifa_world_cup")
        second = await client.get_odds("soccer_fifa_world_cup")
    assert counter["n"] == 1
    assert first == second and len(first) == 1


@pytest.mark.asyncio
async def test_cache_is_keyed_by_sport_and_region():
    """CONTROL: sport_key o región distintos NO comparten entrada (cada uno su crédito)."""
    counter: dict = {}
    async with _client(_counting_handler(counter)) as client:
        await client.get_odds("soccer_fifa_world_cup")
        await client.get_odds("baseball_mlb")  # otro sport → llamada nueva
        await client.get_odds("baseball_mlb", regions="eu")  # otra región → llamada nueva
    assert counter["n"] == 3


@pytest.mark.asyncio
async def test_cache_expires_after_ttl():
    """(c) El caché EXPIRA: pasado el TTL, la próxima llamada vuelve a la API."""
    counter: dict = {}
    t = {"now": 1000.0}
    with patch("src.clients.odds_api.time") as mock_time:
        mock_time.monotonic = lambda: t["now"]
        async with _client(_counting_handler(counter)) as client:
            await client.get_odds("soccer_fifa_world_cup")
            t["now"] += 61.0  # TTL=60 vencido
            await client.get_odds("soccer_fifa_world_cup")
    assert counter["n"] == 2


@pytest.mark.asyncio
async def test_cache_survives_client_recreation():
    """CLAVE del diseño: sources crea un cliente NUEVO por ciclo — el caché es de CLASE
    y sobrevive entre instancias (si fuera de instancia, sería inútil)."""
    counter: dict = {}
    handler = _counting_handler(counter)
    async with _client(handler) as c1:
        await c1.get_odds("soccer_fifa_world_cup")
    async with _client(handler) as c2:  # instancia NUEVA, mismo ciclo de TTL
        await c2.get_odds("soccer_fifa_world_cup")
    assert counter["n"] == 1  # la segunda instancia sirvió del caché de clase


@pytest.mark.asyncio
async def test_quota_breaker_stops_all_calls_and_logs_once():
    """(b) MECANISMO del breaker: tras un 401 OUT_OF_USAGE_CREDITS, get_odds devuelve []
    SIN tocar la red durante el cooldown, con UNA sola línea de log de entrada (mata el
    loop de 544 WARNINGs/día)."""
    from loguru import logger as _logger

    counter: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] = counter.get("n", 0) + 1
        return httpx.Response(
            401, json={"error_code": "OUT_OF_USAGE_CREDITS", "message": "Usage quota reached"}
        )

    records: list[str] = []
    sink = _logger.add(records.append, level="WARNING", format="{message}")
    try:
        with patch("src.clients.odds_api.datetime", _FrozenDatetime):
            async with _client(handler) as client:
                with pytest.raises(OddsAPIAuthError):
                    await client.get_odds("soccer_fifa_world_cup")  # dispara el breaker
                calls_after_trip = counter["n"]
                # Ciclos siguientes (incluso otro sport): [] inmediato, CERO red, CERO logs.
                assert await client.get_odds("soccer_fifa_world_cup") == []
                assert await client.get_odds("baseball_mlb") == []
            assert counter["n"] == calls_after_trip  # ni una llamada más
    finally:
        _logger.remove(sink)
    assert sum(1 for r in records if "CUOTA AGOTADA" in r) == 1  # entrada one-shot


@pytest.mark.asyncio
async def test_quota_breaker_rearms_after_cooldown():
    """El breaker REARMA al vencer el cooldown (con log de salida) y vuelve a la API."""
    counter: dict = {}
    responses = [
        httpx.Response(401, json={"error_code": "OUT_OF_USAGE_CREDITS"}),
        httpx.Response(200, json=_ODDS_FIXTURE),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] = counter.get("n", 0) + 1
        return responses[min(counter["n"] - 1, 1)]

    with patch("src.clients.odds_api.datetime", _FrozenDatetime):
        _FrozenDatetime.current = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
        async with _client(handler) as client:
            with pytest.raises(OddsAPIAuthError):
                await client.get_odds("soccer_fifa_world_cup")
            assert await client.get_odds("soccer_fifa_world_cup") == []  # breaker activo
            _FrozenDatetime.current = datetime(2026, 7, 19, 13, 1, tzinfo=UTC)  # +cooldown
            events = await client.get_odds("soccer_fifa_world_cup")  # rearma → API
    assert len(events) == 1 and counter["n"] == 2


@pytest.mark.asyncio
async def test_quota_breaker_rearms_on_month_change():
    """La cuota de The Odds API resetea por MES: el cambio de mes UTC rearma el breaker
    aunque el cooldown no haya vencido."""
    counter: dict = {}
    responses = [
        httpx.Response(401, json={"error_code": "OUT_OF_USAGE_CREDITS"}),
        httpx.Response(200, json=_ODDS_FIXTURE),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] = counter.get("n", 0) + 1
        return responses[min(counter["n"] - 1, 1)]

    with patch("src.clients.odds_api.datetime", _FrozenDatetime):
        _FrozenDatetime.current = datetime(2026, 7, 31, 23, 59, tzinfo=UTC)
        async with _client(handler) as client:
            with pytest.raises(OddsAPIAuthError):
                await client.get_odds("soccer_fifa_world_cup")
            _FrozenDatetime.current = datetime(2026, 8, 1, 0, 5, tzinfo=UTC)  # +6min, mes NUEVO
            events = await client.get_odds("soccer_fifa_world_cup")
    assert len(events) == 1 and counter["n"] == 2


@pytest.mark.asyncio
async def test_plain_401_does_not_trip_quota_breaker():
    """CONTROL: un 401 común (key inválida) NO activa el breaker de cuota — son fallas
    distintas (la key mala no se arregla esperando el mes)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid API key"})

    async with _client(handler) as client:
        with pytest.raises(OddsAPIAuthError):
            await client.get_odds("soccer_fifa_world_cup")
    assert OddsAPIClient._quota_exhausted_at is None
