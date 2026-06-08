"""
Tests para place_order del KalshiRestClient — serialización de time_in_force.

Fija el contrato del campo time_in_force en el payload del POST:
- explícito "fill_or_kill" se serializa tal cual (lo que usa el arbitraje).
- omitido → default "gtc" (retrocompat; si alguien lo cambia sin querer, falla).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.kalshi_rest import KalshiRestClient, TradingDisabledError


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
        # Colocar un buy (entrada) requiere trading activo (Capa C). Estos tests de
        # serialización operan en ese contexto; explícito para no depender del truthy
        # accidental de MagicMock.
        s.TRADING_ENABLED = True
        m.return_value = s
        yield s


@pytest.mark.asyncio
async def test_place_order_serializes_fill_or_kill(mock_settings, mock_signer):
    """time_in_force='fill_or_kill' se serializa exactamente en el body del POST."""
    client = KalshiRestClient()
    captured: dict = {}

    async def fake_request(method: str, path: str, *, json: dict, **kwargs) -> dict:
        captured["method"] = method
        captured["path"] = path
        captured["json"] = json
        return {"order": {"order_id": "x"}}

    client._request = AsyncMock(side_effect=fake_request)

    await client.place_order(
        ticker="KXTEST",
        side="yes",
        action="buy",
        count=10,
        yes_price=40,
        client_order_id="coid-1",
        time_in_force="fill_or_kill",
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/portfolio/orders"
    assert captured["json"]["time_in_force"] == "fill_or_kill"


@pytest.mark.asyncio
async def test_place_order_defaults_to_gtc(mock_settings, mock_signer):
    """Sin time_in_force explícito → default 'gtc'. Fija el contrato del default."""
    client = KalshiRestClient()
    captured: dict = {}

    async def fake_request(method: str, path: str, *, json: dict, **kwargs) -> dict:
        captured["json"] = json
        return {"order": {"order_id": "x"}}

    client._request = AsyncMock(side_effect=fake_request)

    await client.place_order(
        ticker="KXTEST",
        side="yes",
        action="buy",
        count=10,
        yes_price=40,
    )

    assert captured["json"]["time_in_force"] == "gtc"


@pytest.mark.asyncio
async def test_place_order_blocks_entry_when_trading_disabled(mock_settings, mock_signer):
    """Capa C: una ENTRADA (action='buy') con TRADING_ENABLED=false se bloquea (la orden NUNCA sale)."""
    mock_settings.TRADING_ENABLED = False
    client = KalshiRestClient()
    client._request = AsyncMock()  # no debe alcanzarse

    with pytest.raises(TradingDisabledError):
        await client.place_order(
            ticker="KXTEST",
            side="yes",
            action="buy",
            count=10,
            yes_price=40,
            time_in_force="fill_or_kill",
        )

    client._request.assert_not_called()


@pytest.mark.asyncio
async def test_place_order_allows_protective_sell_when_trading_disabled(mock_settings, mock_signer):
    """Capa C: una SALIDA protectora (action='sell', rollback) NO se bloquea aunque el flag esté off."""
    mock_settings.TRADING_ENABLED = False
    client = KalshiRestClient()
    captured: dict = {}

    async def fake_request(method: str, path: str, *, json: dict, **kwargs) -> dict:
        captured["json"] = json
        return {"order": {"order_id": "x"}}

    client._request = AsyncMock(side_effect=fake_request)

    # Rollback cerrando una pata expuesta: debe poder ejecutarse con el flag en false.
    await client.place_order(
        ticker="KXTEST",
        side="yes",
        action="sell",
        count=10,
        yes_price=1,
        time_in_force="immediate_or_cancel",
    )

    assert captured["json"]["action"] == "sell"
