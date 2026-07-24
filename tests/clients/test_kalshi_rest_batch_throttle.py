"""
Capacidades F2 del cliente (Motor 5): build_order_body compartida, post_only,
batch create/cancel y throttle preventivo de writes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.kalshi_rest import (
    KalshiRestClient,
    TradingDisabledError,
    _WriteThrottle,
    build_order_body,
)


@pytest.fixture
def mock_signer():
    with patch("src.clients.kalshi_rest.KalshiSigner") as m:
        instance = MagicMock()
        instance.sign.return_value = {"KALSHI-ACCESS-KEY": "t", "KALSHI-ACCESS-TIMESTAMP": "1"}
        m.return_value = instance
        yield instance


@pytest.fixture
def mock_settings():
    with patch("src.clients.kalshi_rest.get_settings") as m:
        s = MagicMock()
        s.KALSHI_PRIVATE_KEY_PATH = "/fake/key.pem"
        s.KALSHI_API_KEY_ID = "test-key-id"
        s.rest_url = "https://demo-api.kalshi.co"
        s.TRADING_ENABLED = True
        m.return_value = s
        yield s


def _capturing(client: KalshiRestClient) -> dict:
    captured: dict = {}

    async def fake_request(method: str, path: str, **kwargs) -> dict:
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return {"ok": True}

    client._request = AsyncMock(side_effect=fake_request)
    return captured


# =====================================================
# build_order_body — una sola fuente de verdad del mapeo V2
# =====================================================


@pytest.mark.parametrize(
    ("side", "action", "kw", "book_side", "price"),
    [
        ("yes", "buy", {"yes_price": 47}, "bid", "0.47"),
        ("yes", "sell", {"yes_price": 53}, "ask", "0.53"),
        ("no", "buy", {"no_price": 40}, "ask", "0.60"),
        ("no", "sell", {"no_price": 40}, "bid", "0.60"),
    ],
)
def test_build_order_body_v2_mapping(side, action, kw, book_side, price):
    body = build_order_body(ticker="T", side=side, action=action, count=10, **kw)
    assert body["side"] == book_side and body["price"] == price
    assert body["count"] == "10" and body["time_in_force"] == "good_till_canceled"
    assert "post_only" not in body  # sin pedirlo, el payload no cambia ni un byte


def test_build_order_body_post_only_flag():
    body = build_order_body(
        ticker="T", side="yes", action="buy", count=1, yes_price=47, post_only=True
    )
    assert body["post_only"] is True


def test_build_order_body_rejects_out_of_range():
    with pytest.raises(ValueError, match="fuera de rango"):
        build_order_body(ticker="T", side="no", action="buy", count=1, no_price=100)


# =====================================================
# post_only en place_order (passthrough)
# =====================================================


@pytest.mark.asyncio
async def test_place_order_forwards_post_only(mock_settings, mock_signer):
    client = KalshiRestClient()
    captured = _capturing(client)
    await client.place_order(
        ticker="T", side="yes", action="buy", count=5, yes_price=47, post_only=True
    )
    assert captured["json"]["post_only"] is True
    assert captured["path"] == "/portfolio/events/orders"


# =====================================================
# batch create/cancel
# =====================================================


@pytest.mark.asyncio
async def test_batch_create_blocked_without_trading(mock_settings, mock_signer):
    """Guard MÁS estricto que Capa C: un ask del MM abre posición NO (no es sell
    protector) → con trading off se bloquea TODO el batch."""
    mock_settings.TRADING_ENABLED = False
    client = KalshiRestClient()
    _capturing(client)
    order = build_order_body(ticker="T", side="yes", action="sell", count=1, yes_price=53)
    with pytest.raises(TradingDisabledError):
        await client.batch_create_orders([order])


@pytest.mark.asyncio
async def test_batch_create_posts_batched_endpoint(mock_settings, mock_signer):
    client = KalshiRestClient()
    captured = _capturing(client)
    orders = [
        build_order_body(ticker="T", side="yes", action="buy", count=1, yes_price=47),
        build_order_body(ticker="T", side="yes", action="sell", count=1, yes_price=53),
    ]
    await client.batch_create_orders(orders)
    assert captured["method"] == "POST" and captured["path"] == "/portfolio/orders/batched"
    assert captured["json"] == {"orders": orders}


@pytest.mark.asyncio
async def test_batch_cancel_always_allowed(mock_settings, mock_signer):
    """Cancelar es protector → sin gate de trading (pieza del cancel-all del kill-switch)."""
    mock_settings.TRADING_ENABLED = False
    client = KalshiRestClient()
    captured = _capturing(client)
    await client.batch_cancel_orders(["a", "b"])
    assert captured["method"] == "DELETE" and captured["path"] == "/portfolio/orders/batched"
    assert captured["json"] == {"ids": ["a", "b"]}


@pytest.mark.asyncio
async def test_batch_empty_are_noops(mock_settings, mock_signer):
    client = KalshiRestClient()
    captured = _capturing(client)
    await client.batch_cancel_orders([])
    assert "method" not in captured  # ni un write a la red por un batch vacío


# =====================================================
# _WriteThrottle — token bucket determinístico
# =====================================================


@pytest.mark.asyncio
async def test_throttle_burst_then_waits():
    clock = {"t": 0.0}
    waits: list[float] = []

    async def fake_sleep(s: float) -> None:
        waits.append(s)
        clock["t"] += s

    th = _WriteThrottle(
        rate_per_sec=5.0, burst=5.0, time_fn=lambda: clock["t"], sleep_fn=fake_sleep
    )
    for _ in range(5):
        await th.acquire()  # burst completo sin espera
    assert waits == []
    await th.acquire()  # el 6to en t=0 debe esperar 1/5s
    assert waits == [pytest.approx(0.2)]


@pytest.mark.asyncio
async def test_throttle_refills_with_time():
    clock = {"t": 0.0}
    waits: list[float] = []

    async def fake_sleep(s: float) -> None:
        waits.append(s)
        clock["t"] += s

    th = _WriteThrottle(
        rate_per_sec=5.0, burst=5.0, time_fn=lambda: clock["t"], sleep_fn=fake_sleep
    )
    for _ in range(5):
        await th.acquire()
    clock["t"] += 1.0  # 1s después: +5 tokens
    for _ in range(5):
        await th.acquire()
    assert waits == []


@pytest.mark.asyncio
async def test_throttle_batch_acquires_n():
    clock = {"t": 0.0}
    waits: list[float] = []

    async def fake_sleep(s: float) -> None:
        waits.append(s)
        clock["t"] += s

    th = _WriteThrottle(
        rate_per_sec=5.0, burst=5.0, time_fn=lambda: clock["t"], sleep_fn=fake_sleep
    )
    await th.acquire(5)  # consume el burst entero
    await th.acquire(5)  # necesita 5 tokens más → 1s
    assert waits == [pytest.approx(1.0)]
