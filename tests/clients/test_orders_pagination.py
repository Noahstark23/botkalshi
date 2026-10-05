"""La reconciliación nunca debe consumir una colección incompleta de órdenes."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.clients.kalshi_rest import KalshiRestClient


@pytest.fixture
def client():
    # No construir signer/settings: todos los transportes son falsos, sin secretos.
    instance = object.__new__(KalshiRestClient)
    instance._request = AsyncMock()
    return instance


async def test_get_orders_passes_opaque_cursor_without_changing_other_filters(client):
    client._request.return_value = {"orders": [], "cursor": ""}
    await client.get_orders(ticker="T", status="resting", limit=20, cursor="page+/=2")
    client._request.assert_awaited_once_with(
        "GET",
        "/portfolio/orders",
        params={"ticker": "T", "status": "resting", "limit": 20, "cursor": "page+/=2"},
    )


async def test_get_all_orders_reads_second_page_and_preserves_filters(client):
    client._request.side_effect = [
        {"orders": [{"order_id": "a"}], "cursor": "next"},
        {"orders": [{"order_id": "b"}], "cursor": ""},
    ]
    assert await client.get_all_orders(ticker="T", status="resting", limit=1) == [
        {"order_id": "a"},
        {"order_id": "b"},
    ]
    assert client._request.await_args_list[0].kwargs["params"] == {
        "ticker": "T",
        "status": "resting",
        "limit": 1,
    }
    assert client._request.await_args_list[1].kwargs["params"] == {
        "ticker": "T",
        "status": "resting",
        "limit": 1,
        "cursor": "next",
    }


async def test_empty_page_with_cursor_is_not_end_of_collection(client):
    client._request.side_effect = [
        {"orders": [], "cursor": "next"},
        {"orders": [{"order_id": "a"}], "cursor": ""},
    ]
    assert await client.get_all_orders() == [{"order_id": "a"}]
    assert client._request.await_count == 2


async def test_empty_final_collection_is_valid(client):
    client._request.return_value = {"orders": [], "cursor": ""}
    assert await client.get_all_orders() == []


@pytest.mark.parametrize(
    "page",
    [
        None,
        [],
        {"cursor": ""},
        {"orders": None, "cursor": ""},
        {"orders": {}, "cursor": ""},
        {"orders": [None], "cursor": ""},
        {"orders": ["a"], "cursor": ""},
        {"orders": []},
        {"orders": [], "cursor": None},
        {"orders": [], "cursor": 1},
        {"orders": [], "cursor": "x" * 4097},
    ],
)
async def test_malformed_page_never_returns_partial_orders(client, page):
    client._request.side_effect = [
        {"orders": [{"order_id": "a"}], "cursor": "next"},
        page,
    ]
    with pytest.raises(RuntimeError):
        await client.get_all_orders()


@pytest.mark.parametrize("cursors", [["same", "same"], ["a", "b", "a"]])
async def test_cursor_cycles_fail_in_bounded_requests(client, cursors):
    client._request.side_effect = [{"orders": [], "cursor": cursor} for cursor in cursors]
    with pytest.raises(RuntimeError, match="cursor"):
        await client.get_all_orders()
    assert client._request.await_count == len(cursors)


async def test_page_cap_does_not_return_partial_collection(client):
    client._request.side_effect = [
        {"orders": [{"order_id": "a"}], "cursor": "one"},
        {"orders": [{"order_id": "b"}], "cursor": "two"},
    ]
    with pytest.raises(RuntimeError, match="max_pages"):
        await client.get_all_orders(max_pages=2)
    assert client._request.await_count == 2


async def test_final_page_at_cap_is_complete(client):
    client._request.return_value = {"orders": [{"order_id": "a"}], "cursor": ""}
    assert await client.get_all_orders(max_pages=1) == [{"order_id": "a"}]


async def test_page_larger_than_requested_is_rejected(client):
    client._request.return_value = {"orders": [{}, {}], "cursor": ""}
    with pytest.raises(RuntimeError, match="limit"):
        await client.get_all_orders(limit=1)


async def test_request_error_on_second_page_propagates_without_partial_result(client):
    error = TimeoutError("red interrumpida")
    client._request.side_effect = [
        {"orders": [{"order_id": "a"}], "cursor": "next"},
        error,
    ]
    with pytest.raises(TimeoutError) as caught:
        await client.get_all_orders()
    assert caught.value is error


async def test_total_deadline_cancels_collection_without_partial_result(client, monkeypatch):
    monkeypatch.setattr("src.clients.kalshi_rest._ORDERS_PAGINATION_TIMEOUT_SECONDS", 0.001)

    async def slow_second_page(*args, **kwargs):
        if not kwargs["params"].get("cursor"):
            return {"orders": [{"order_id": "a"}], "cursor": "next"}
        await asyncio.sleep(1)
        return {"orders": [], "cursor": ""}

    client._request.side_effect = slow_second_page
    with pytest.raises(TimeoutError):
        await client.get_all_orders()
    assert client._request.await_count == 2


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 1.0, "10", None])
async def test_get_orders_rejects_invalid_limit_before_request(client, limit):
    with pytest.raises(ValueError, match="limit"):
        await client.get_orders(limit=limit)
    client._request.assert_not_awaited()


@pytest.mark.parametrize("cursor", [1, True, [], "x" * 4097])
async def test_get_orders_rejects_invalid_cursor_before_request(client, cursor):
    with pytest.raises(ValueError, match="cursor"):
        await client.get_orders(cursor=cursor)
    client._request.assert_not_awaited()


@pytest.mark.parametrize("max_pages", [0, -1, 101, True, 1.0, "2", None])
async def test_get_all_orders_rejects_invalid_page_cap_before_request(client, max_pages):
    with pytest.raises(ValueError, match="max_pages"):
        await client.get_all_orders(max_pages=max_pages)
    client._request.assert_not_awaited()


@pytest.mark.parametrize("limit", [1, 1000])
async def test_get_orders_accepts_limit_boundaries(client, limit):
    client._request.return_value = {"orders": [], "cursor": ""}
    assert await client.get_all_orders(limit=limit) == []
