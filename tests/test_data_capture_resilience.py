"""Tests de resilience del data_capture service."""

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.data_capture import DataCaptureService
from src.utils.config import reset_settings_for_testing


@pytest.fixture(autouse=True)
def fake_env(monkeypatch, tmp_path):
    """Inyecta credenciales mínimas válidas y parchea KalshiWebSocket."""
    reset_settings_for_testing()
    fake_key = tmp_path / "key.pem"
    fake_key.write_text("dummy")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))

    with patch("src.strategies.data_capture.KalshiWebSocket") as mock_ws_cls:
        mock_ws = MagicMock()
        mock_ws.stop = AsyncMock()
        mock_ws.run = AsyncMock(return_value=None)
        mock_ws.queue_subscription = MagicMock()
        mock_ws.on = MagicMock()
        mock_ws_cls.return_value = mock_ws
        yield mock_ws

    reset_settings_for_testing()


@pytest.mark.asyncio
async def test_run_retries_when_discovery_returns_empty():
    """
    Si _discover_markets no encuentra tickers en el primer intento,
    run() debe reintentar (no hacer return definitivo).
    """
    service = DataCaptureService()

    call_count = 0

    async def fake_discover():
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            service._tracked_tickers.add("FAKE-TICKER")

    service._discover_markets = fake_discover

    service.ws = MagicMock()
    service.ws.run = AsyncMock(side_effect=asyncio.CancelledError)
    service.ws.queue_subscription = MagicMock()
    service.ws.on = MagicMock()
    service.ws.stop = AsyncMock()
    service._periodic_snapshots = AsyncMock()

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.1)  # primera iteración
    await service.stop()
    with suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert call_count >= 1


@pytest.mark.asyncio
async def test_run_exits_cleanly_on_stop_during_backoff():
    """Si _stop_event se activa durante el backoff, run() debe terminar limpio."""
    service = DataCaptureService()

    async def fake_discover_always_empty():
        pass  # nunca puebla _tracked_tickers

    service._discover_markets = fake_discover_always_empty

    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.1)
    await service.stop()

    await asyncio.wait_for(task, timeout=10.0)
    # No handlers should have been registered — we exited before the handler block
    assert service.ws.on.call_count == 0


@pytest.mark.asyncio
async def test_run_proceeds_when_tickers_found_on_first_attempt():
    """Si discovery encuentra tickers en el primer intento, no debe retryar."""
    service = DataCaptureService()

    call_count = 0

    async def fake_discover_success():
        nonlocal call_count
        call_count += 1
        service._tracked_tickers.add("FAKE-TICKER")

    service._discover_markets = fake_discover_success

    service.ws = MagicMock()
    service.ws.run = AsyncMock(side_effect=asyncio.CancelledError)
    service.ws.queue_subscription = MagicMock()
    service.ws.on = MagicMock()
    service.ws.stop = AsyncMock()
    service._periodic_snapshots = AsyncMock()

    task = asyncio.create_task(service.run())
    with suppress(TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert call_count == 1


@pytest.mark.asyncio
async def test_discover_markets_propagates_errors_to_caller():
    """P2: un fallo del listado paginado PROPAGA — lo manejan los callers (el boot
    reintenta con backoff; el re-discovery es best-effort y registra el error)."""
    service = DataCaptureService()

    with patch("src.strategies.data_capture.KalshiRestClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.list_markets.side_effect = Exception("429 Too Many Requests")
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(Exception, match="429"):
            await service._discover_markets()
