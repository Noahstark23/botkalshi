"""El cliente readonly bloquea escrituras antes de cualquier acceso HTTP."""

from unittest.mock import AsyncMock, patch

import pytest

from src.clients.kalshi_readonly import ReadOnlyKalshiRestClient
from src.clients.kalshi_rest import KalshiRestClient


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/portfolio/orders"),
        ("DELETE", "/portfolio/orders/o1"),
        ("PUT", "/portfolio/order_groups/g1/reset"),
        ("GET", "/anything"),
    ],
)
async def test_rejects_non_allowlisted_requests_before_parent(method, path):
    c = object.__new__(ReadOnlyKalshiRestClient)
    with patch.object(KalshiRestClient, "_request", new_callable=AsyncMock) as request:
        with pytest.raises(PermissionError):
            await c._request(method, path)
        request.assert_not_awaited()


async def test_allows_only_portfolio_get_and_keeps_scope_and_cursor():
    c = object.__new__(ReadOnlyKalshiRestClient)
    with patch.object(KalshiRestClient, "_request", new_callable=AsyncMock) as request:
        request.return_value = {"fills": [], "cursor": ""}
        await c.get_fills(cursor="page2", min_ts=1, max_ts=2, subaccount=3)
        request.assert_awaited_once_with(
            "GET",
            "/portfolio/fills",
            params={"limit": 100, "cursor": "page2", "min_ts": 1, "max_ts": 2, "subaccount": 3},
        )


async def test_legacy_calls_keep_their_original_payloads():
    c = object.__new__(KalshiRestClient)
    c._request = AsyncMock(return_value={})
    await c.get_balance()
    c._request.assert_awaited_with("GET", "/portfolio/balance")
    await c.get_orders(limit=20)
    c._request.assert_awaited_with("GET", "/portfolio/orders", params={"limit": 20})
    await c.get_positions(cursor="next", subaccount=2)
    c._request.assert_awaited_with(
        "GET", "/portfolio/positions", params={"limit": 100, "cursor": "next", "subaccount": 2}
    )
    await c.get_settlements(cursor="next", subaccount=2)
    c._request.assert_awaited_with(
        "GET", "/portfolio/settlements", params={"limit": 200, "cursor": "next", "subaccount": 2}
    )
