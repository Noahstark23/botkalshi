"""Una SELL puede abrir NO: sólo una salida reduce_only puede pasar con trading OFF."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.clients.kalshi_rest import KalshiRestClient, TradingDisabledError, build_order_body


@pytest.fixture
def client():
    with (
        patch("src.clients.kalshi_rest.get_settings"),
        patch("src.clients.kalshi_rest.KalshiSigner"),
    ):
        instance = KalshiRestClient()
    instance.settings = SimpleNamespace(TRADING_ENABLED=False)
    instance._request = AsyncMock(return_value={"order_id": "test-order"})
    instance._write_throttle = AsyncMock()
    return instance


@pytest.mark.asyncio
async def test_unrestricted_sell_with_trading_off_never_reaches_http(client):
    with pytest.raises(TradingDisabledError):
        await client.place_order(ticker="TEST", side="yes", action="sell", count=1, yes_price=50)
    client._request.assert_not_awaited()
    client._write_throttle.acquire.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "side,field,book_side", [("yes", "yes_price", "ask"), ("no", "no_price", "bid")]
)
async def test_protective_close_is_reduce_only_ioc_on_wire(client, side, field, book_side):
    await client.place_order(
        ticker="TEST",
        side=side,
        action="sell",
        count=1,
        **{field: 50},
        time_in_force="immediate_or_cancel",
        reduce_only=True,
    )
    body = client._request.await_args.kwargs["json"]
    assert body["reduce_only"] is True
    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["side"] == book_side


@pytest.mark.parametrize(
    "extra",
    [
        {"time_in_force": "gtc"},
        {"time_in_force": "fill_or_kill"},
        {"post_only": True},
        {"action": "buy"},
        {"reduce_only": "true"},
    ],
)
def test_invalid_protective_order_rejected_before_network(extra):
    kwargs = {
        "ticker": "TEST",
        "side": "yes",
        "action": "sell",
        "count": 1,
        "yes_price": 50,
        "reduce_only": True,
        "time_in_force": "immediate_or_cancel",
    }
    kwargs.update(extra)
    with pytest.raises(ValueError):
        build_order_body(**kwargs)


@pytest.mark.asyncio
async def test_entry_blocked_if_flag_changes_while_waiting_for_rate_limit(client):
    client.settings.TRADING_ENABLED = True

    async def disable():
        client.settings.TRADING_ENABLED = False

    client._write_throttle.acquire.side_effect = disable
    with pytest.raises(TradingDisabledError):
        await client.place_order(ticker="TEST", side="yes", action="buy", count=1, yes_price=50)
    client._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_market_maker_sell_remains_explicitly_risk_increasing(client):
    client.settings.TRADING_ENABLED = True
    await client.place_order(
        ticker="TEST", side="yes", action="sell", count=1, yes_price=50, post_only=True
    )
    assert client._request.await_args.kwargs["json"].get("reduce_only", False) is False
