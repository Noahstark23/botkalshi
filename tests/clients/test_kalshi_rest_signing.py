"""
Regresión del bug de firma (2026-06-17, plata real): la firma de Kalshi va sobre
`timestamp + method + path` SIN el query string. Incluir el ?qs daba 401
INCORRECT_API_KEY_SIGNATURE en endpoints autenticados con params (fills/positions),
dejando al bot CIEGO a su cartera mientras place_order (sin query) sí firmaba.

Fija el contrato: el path firmado NO lleva ?qs, pero el request SÍ manda los params.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.kalshi_rest import KalshiRestClient


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


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"market_positions": []}
    return resp


@pytest.mark.asyncio
async def test_signed_path_excludes_querystring_but_request_keeps_params(
    mock_settings, mock_signer
):
    """positions?limit=100: la FIRMA va sobre el path base; el QS va solo en la URL del request."""
    client = KalshiRestClient()
    fake_http = MagicMock()
    fake_http.request = AsyncMock(return_value=_ok_response())
    client._client = fake_http  # bypass __aenter__

    await client.get_positions(limit=100)

    # 1) La firma fue sobre el path BASE — sin '?qs' (la causa del 401).
    signed_path = mock_signer.sign.call_args.args[1]
    assert signed_path == "/trade-api/v2/portfolio/positions"
    assert "?" not in signed_path

    # 2) Pero el request SÍ envía los params (httpx los pone en la URL).
    assert fake_http.request.call_args.kwargs["params"] == {"limit": 100}


@pytest.mark.asyncio
async def test_no_params_endpoint_signs_base_path(mock_settings, mock_signer):
    """balance (sin params) firma el path base — el caso que SIEMPRE funcionó."""
    client = KalshiRestClient()
    fake_http = MagicMock()
    fake_http.request = AsyncMock(return_value=_ok_response())
    client._client = fake_http

    await client.get_balance()

    assert mock_signer.sign.call_args.args[1] == "/trade-api/v2/portfolio/balance"
