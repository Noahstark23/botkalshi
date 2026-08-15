"""
Tests de place_order del KalshiRestClient — contrato V2 (POST /portfolio/events/orders).

Kalshi deprecó el endpoint legacy /portfolio/orders (410). V2 cotiza desde el libro YES
con side bid/ask y un único price en DÓLARES string. Estos tests fijan:
- el endpoint y el shape V2 (side, price, count como strings; sin action/type),
- el MAPEO real-money (side yes/no + action buy/sell) → (bid/ask, precio en dólares),
- la serialización de time_in_force (FOK del arbitraje; default normalizado),
- la Capa C (bloquea entradas con TRADING_ENABLED=false; permite el sell protector).
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
        # Colocar un buy (entrada) requiere trading activo (Capa C); explícito para no
        # depender del truthy accidental de MagicMock.
        s.TRADING_ENABLED = True
        m.return_value = s
        yield s


def _capturing_client() -> tuple[KalshiRestClient, dict]:
    client = KalshiRestClient()
    captured: dict = {}

    async def fake_request(method: str, path: str, *, json: dict, **kwargs) -> dict:
        captured["method"] = method
        captured["path"] = path
        captured["json"] = json
        return {"order_id": "x", "fill_count": "0.00", "remaining_count": "10.00"}

    client._request = AsyncMock(side_effect=fake_request)
    return client, captured


@pytest.mark.asyncio
async def test_place_order_uses_v2_endpoint_and_shape(mock_settings, mock_signer):
    """V2: POST /portfolio/events/orders, side bid/ask, price/count strings, sin action/type."""
    client, captured = _capturing_client()

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
    assert captured["path"] == "/portfolio/events/orders"
    body = captured["json"]
    assert body["time_in_force"] == "fill_or_kill"
    assert body["side"] == "bid"  # comprar YES
    assert body["price"] == "0.40"  # dólares string
    assert body["count"] == "10"  # FixedPointCount string
    assert body["client_order_id"] == "coid-1"
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    assert "action" not in body and "type" not in body  # V2 no los tiene
    assert "yes_price" not in body and "no_price" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("side", "action", "yes_price", "no_price", "exp_side", "exp_price"),
    [
        ("yes", "buy", 40, None, "bid", "0.40"),  # comprar YES @ 40c
        ("yes", "sell", 1, None, "ask", "0.01"),  # vender YES @ 1c (rollback)
        ("no", "buy", None, 48, "ask", "0.52"),  # comprar NO @ 48c ≡ vender YES @ 52c
        ("no", "sell", None, 1, "bid", "0.99"),  # vender NO @ 1c ≡ comprar YES @ 99c (rollback)
    ],
)
async def test_place_order_maps_side_action_to_v2(
    mock_settings, mock_signer, side, action, yes_price, no_price, exp_side, exp_price
):
    """EL MAPEO REAL-MONEY: cada (side, action) → el (bid/ask, precio en dólares) correcto."""
    client, captured = _capturing_client()

    await client.place_order(
        ticker="KXTEST",
        side=side,
        action=action,
        count=5,
        yes_price=yes_price,
        no_price=no_price,
        client_order_id="coid",
        time_in_force="immediate_or_cancel",
    )

    assert captured["json"]["side"] == exp_side
    assert captured["json"]["price"] == exp_price


@pytest.mark.asyncio
async def test_place_order_normalizes_gtc_default(mock_settings, mock_signer):
    """Sin time_in_force explícito → 'gtc' se normaliza al valor V2 'good_till_canceled'."""
    client, captured = _capturing_client()

    await client.place_order(ticker="KXTEST", side="yes", action="buy", count=10, yes_price=40)

    assert captured["json"]["time_in_force"] == "good_till_canceled"


@pytest.mark.asyncio
async def test_place_order_price_dollar_format(mock_settings, mock_signer):
    """El precio se serializa en dólares exactos (Decimal): 5c → '0.05', 99c → '0.99'."""
    client, captured = _capturing_client()
    await client.place_order(
        ticker="T", side="yes", action="buy", count=1, yes_price=5, client_order_id="c"
    )
    assert captured["json"]["price"] == "0.05"


@pytest.mark.asyncio
async def test_place_order_blocks_entry_when_trading_disabled(mock_settings, mock_signer):
    """Capa C: una ENTRADA (action='buy') con TRADING_ENABLED=false se bloquea (no sale)."""
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
    """Capa C: una SALIDA protectora (action='sell', rollback) NO se bloquea con el flag off."""
    mock_settings.TRADING_ENABLED = False
    client, captured = _capturing_client()

    # Rollback cerrando una pata YES expuesta: vender YES @ 1c → ask.
    await client.place_order(
        ticker="KXTEST",
        side="yes",
        action="sell",
        count=10,
        yes_price=1,
        time_in_force="immediate_or_cancel",
    )

    assert captured["json"]["side"] == "ask"  # vender YES
    assert captured["json"]["price"] == "0.01"


@pytest.mark.asyncio
async def test_el_sell_pasa_con_trading_off_solo_por_el_invariante_binario(
    mock_settings, mock_signer
):
    """PIN del ALCANCE del invariante (2026-08-15). La Capa C deja pasar `sell` con
    TRADING_ENABLED=false porque en BINARIOS un sell solo puede CERRAR una posición
    (salida protectora del rollback). Desde que Kalshi lista perpetuos apalancados
    (3-jun-2026) eso dejó de ser cierto para el EXCHANGE: ahí un sell ABRE un corto.
    El bot no tiene una línea de perps, así que la excepción sigue siendo segura —
    este test existe para dejar EXPLÍCITO qué la sostiene: si alguna vez entra un
    producto con posición corta, este comportamiento se cambia ANTES que nada."""
    mock_settings.TRADING_ENABLED = False
    client = KalshiRestClient()
    client._request = AsyncMock(return_value={"order": {"order_id": "x"}})

    with pytest.raises(TradingDisabledError):  # buy = ENTRADA → bloqueada
        await client.place_order(
            ticker="KXTEST",
            side="yes",
            action="buy",
            count=1,
            yes_price=50,
            client_order_id="c1",
        )
    client._request.assert_not_awaited()

    # sell = SALIDA protectora → pasa. Válido SOLO mientras el universo sea binario.
    await client.place_order(
        ticker="KXTEST",
        side="yes",
        action="sell",
        count=1,
        yes_price=50,
        client_order_id="c2",
    )
    client._request.assert_awaited_once()
