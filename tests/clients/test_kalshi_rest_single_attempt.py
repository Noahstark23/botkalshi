"""Una respuesta perdida no autoriza otra mutación; GET/HEAD sí conservan backoff.

Transporte y firma ficticios: ninguna credencial, socket u orden del exchange.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.clients.kalshi_rest import (
    KalshiRateLimitError,
    KalshiRestClient,
    KalshiServerError,
)


@pytest.fixture
def client():
    settings = MagicMock()
    settings.TRADING_ENABLED = True
    settings.KALSHI_PRIVATE_KEY_PATH = "/nonexistent/test-only.pem"
    settings.KALSHI_API_KEY_ID = "test-only"
    with (
        patch("src.clients.kalshi_rest.get_settings", return_value=settings),
        patch("src.clients.kalshi_rest.KalshiSigner") as signer,
        patch("src.clients.kalshi_rest._record_api_error") as record_error,
    ):
        signer.return_value.sign.return_value = {"X-Test-Only": "no-key"}
        instance = KalshiRestClient()
        instance._client = AsyncMock(spec=httpx.AsyncClient)
        instance._write_throttle = AsyncMock()
        yield instance, record_error


def response(status=200):
    return httpx.Response(status, json={"result": "accepted-by-fake-transport"})


FAILURES = (
    "read_timeout",
    "write_timeout",
    "connect_error",
    "read_error",
    "429",
    "500",
    "503",
)


def failure(kind):
    exceptions = {
        "read_timeout": httpx.ReadTimeout,
        "write_timeout": httpx.WriteTimeout,
        "connect_error": httpx.ConnectError,
        "read_error": httpx.ReadError,
    }
    if kind in exceptions:
        error_type = exceptions[kind]
        return error_type("resultado desconocido del transporte ficticio"), error_type
    return response(int(kind)), KalshiRateLimitError if kind == "429" else KalshiServerError


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "DELETE", "PATCH", "PUT", "post"])
@pytest.mark.parametrize("kind", FAILURES)
async def test_mutation_failure_never_reissues_request(client, method, kind):
    instance, record_error = client
    first, error_type = failure(kind)
    instance._client.request.side_effect = [first, response()]
    with patch("asyncio.sleep", new=AsyncMock()) as sleep, pytest.raises(error_type):
        await instance._request(method, "/portfolio/events/orders", json={"test": True})
    assert instance._client.request.await_count == 1
    assert instance.signer.sign.call_count == 1
    sleep.assert_not_awaited()
    record_error.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("kind", FAILURES)
async def test_reads_keep_bounded_retries(client, method, kind):
    instance, record_error = client
    first, _ = failure(kind)
    instance._client.request.side_effect = [first, first, first, response()]
    with patch("asyncio.sleep", new=AsyncMock()) as sleep:
        result = await instance._request(method, "/portfolio/orders")
    assert result == {"result": "accepted-by-fake-transport"}
    assert instance._client.request.await_count == 4
    assert instance.signer.sign.call_count == 4
    assert [call.args[0] for call in sleep.await_args_list] == [1, 2, 4]
    record_error.assert_not_called()


@pytest.mark.asyncio
async def test_read_exhaustion_preserves_error_and_bound(client):
    instance, record_error = client
    instance._client.request.side_effect = httpx.ReadTimeout("ficticio")
    with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(httpx.ReadTimeout):
        await instance.get_balance()
    assert instance._client.request.await_count == 4
    record_error.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True])
async def test_public_create_path_does_not_repeat_lost_ack(client, batch):
    instance, record_error = client
    instance._client.request.side_effect = [httpx.ReadTimeout("ACK perdido"), response()]
    raised = None
    with patch("asyncio.sleep", new=AsyncMock()):
        try:
            if batch:
                await instance.batch_create_orders([{"client_order_id": "fake-batch"}])
            else:
                await instance.place_order(
                    ticker="FAKE-ONLY",
                    side="yes",
                    action="buy",
                    count=1,
                    yes_price=30,
                    time_in_force="immediate_or_cancel",
                    client_order_id="fake-single",
                )
        except httpx.ReadTimeout as exc:
            raised = exc
    assert instance._client.request.await_count == 1
    assert isinstance(raised, httpx.ReadTimeout)
    expected = "/portfolio/events/orders/batched" if batch else "/portfolio/events/orders"
    assert instance._client.request.await_args.args == ("POST", expected)
    record_error.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_success_and_cancellation_are_not_replayed(client, method):
    instance, record_error = client
    instance._client.request.return_value = response()
    assert await instance._request(method, "/test") == {"result": "accepted-by-fake-transport"}
    instance._client.request.assert_awaited_once()
    instance._client.request.reset_mock()
    instance._client.request.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await instance._request(method, "/test")
    instance._client.request.assert_awaited_once()
    record_error.assert_not_called()
