"""Contrato de lectura de fills, sin constructor, firmas ni transporte."""

from unittest.mock import AsyncMock

from src.clients.kalshi_rest import KalshiRestClient


async def test_live_fills_filter_order_without_changing_route():
    client = object.__new__(KalshiRestClient)
    client._request = AsyncMock(return_value={"fills": [], "cursor": ""})
    await client.get_fills(ticker="T", order_id="OID", subaccount=0, limit=100, cursor="next")
    client._request.assert_awaited_once_with(
        "GET",
        "/portfolio/fills",
        params={"ticker": "T", "order_id": "OID", "subaccount": 0, "limit": 100, "cursor": "next"},
    )


async def test_historical_fills_use_documented_filters_only():
    client = object.__new__(KalshiRestClient)
    client._request = AsyncMock(return_value={"fills": [], "cursor": ""})
    await client.get_historical_fills(ticker="T", subaccount=0, limit=100, cursor="next")
    client._request.assert_awaited_once_with(
        "GET",
        "/historical/fills",
        params={"ticker": "T", "subaccount": 0, "limit": 100, "cursor": "next"},
    )
