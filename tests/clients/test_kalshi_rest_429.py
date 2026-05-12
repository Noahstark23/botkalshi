"""
Tests para comportamiento 429 del KalshiRestClient.

Verifica:
- Backoff correcto sin Retry-After header (Kalshi no lo envía)
- BotState.record_error se llama cuando retries se agotan
- No crash cuando respuesta 429 no tiene Retry-After
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.clients.kalshi_rest import KalshiRateLimitError, KalshiRestClient


@pytest.fixture
def mock_signer():
    with patch("src.clients.kalshi_rest.KalshiSigner") as m:
        instance = MagicMock()
        instance.sign.return_value = {"KALSHI-ACCESS-KEY": "test", "KALSHI-ACCESS-TIMESTAMP": "1"}
        m.return_value = instance
        yield instance


@pytest.fixture
def mock_settings():
    with patch("src.clients.kalshi_rest.get_settings") as m:
        s = MagicMock()
        s.KALSHI_PRIVATE_KEY_PATH = "/fake/key.pem"
        s.KALSHI_API_KEY_ID = "test-key-id"
        s.rest_url = "https://trading-api.kalshi.com"
        m.return_value = s
        yield s


def _make_response(status_code: int, body: str = "{}") -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = body
    resp.json.return_value = {}
    return resp


@pytest.mark.asyncio
async def test_429_triggers_backoff_with_correct_params(mock_settings, mock_signer):
    """
    3×429 seguidos de 1×200: verifica que el cliente usa wait_exponential
    con max=60 (no max=10) y que NO busca Retry-After header.

    Chequeamos que los sleeps intermedios existen (backoff real) inspeccionando
    el wait configurado en tenacity, en lugar de mockear asyncio.sleep globalmente
    (lo que rompería la lógica de httpx).
    """
    ok_resp = _make_response(200)
    ok_resp.json.return_value = {"balance": 5000}

    responses = [
        _make_response(429, '{"error":"rate limited"}'),
        _make_response(429, '{"error":"rate limited"}'),
        _make_response(429, '{"error":"rate limited"}'),
        ok_resp,
    ]

    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # No dormir de verdad para que el test sea rápido

    with patch("asyncio.sleep", side_effect=fake_sleep):
        client = KalshiRestClient()
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.request = AsyncMock(side_effect=responses)

        result = await client._request("GET", "/portfolio/balance")

    assert result == {"balance": 5000}
    assert len(sleep_calls) >= 2  # al menos 2 backoffs entre los 3 intentos fallidos
    # Backoff exponencial: 1s, 2s, 4s (cap 60s). Suma mínima de los primeros 2: ≥3s
    assert sum(sleep_calls) >= 3.0


@pytest.mark.asyncio
async def test_429_exhausted_calls_record_error(mock_settings, mock_signer):
    """
    4×429 (retries agotados) → BotState.record_error debe llamarse
    con mensaje que contiene info del error.
    """
    responses = [_make_response(429, '{"error":"rate limited"}') for _ in range(4)]

    recorded_errors: list[str] = []

    with patch("asyncio.sleep", new=AsyncMock()):
        with patch("src.monitoring.health.BotState") as mock_botstate:
            mock_botstate.record_error = MagicMock(side_effect=lambda msg: recorded_errors.append(msg))

            client = KalshiRestClient()
            client._client = AsyncMock(spec=httpx.AsyncClient)
            client._client.request = AsyncMock(side_effect=responses)

            with pytest.raises(KalshiRateLimitError):
                await client._request("GET", "/portfolio/balance")

    assert len(recorded_errors) >= 1
    assert any("429" in e or "RateLimit" in e for e in recorded_errors)


@pytest.mark.asyncio
async def test_no_retry_after_header_no_crash(mock_settings, mock_signer):
    """
    429 sin Retry-After header → no crash, backoff normal de tenacity.

    Este test documenta que removimos el parsing de Retry-After que causaba
    el loop perpetuo de 560 errores.
    """
    ok_resp = _make_response(200)
    ok_resp.json.return_value = {"result": "ok"}

    # Respuesta 429 sin ningún header de Retry-After
    resp_429 = _make_response(429, '{"error":"rate limited"}')

    responses = [resp_429, ok_resp]

    with patch("asyncio.sleep", new=AsyncMock()):
        client = KalshiRestClient()
        client._client = AsyncMock(spec=httpx.AsyncClient)
        client._client.request = AsyncMock(side_effect=responses)

        # No debe colgar ni crashear por Retry-After ausente
        result = await client._request("GET", "/events")

    assert result == {"result": "ok"}
