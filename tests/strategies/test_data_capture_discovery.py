"""
Tests para DataCaptureService._discover_markets.

Verifica:
- asyncio.sleep(2.0) se llama después de cada prefix (anti-burst)
- Un prefix fallido no aborta los demás (loop continúa)
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.data_capture import DataCaptureService


@pytest.fixture
def service():
    with patch("src.strategies.data_capture.get_settings"), patch(
        "src.strategies.data_capture.KalshiWebSocket"
    ):
        svc = DataCaptureService()
        svc._tracked_tickers = set()
        return svc


def _make_events_resp(prefix: str, n_markets: int = 2) -> dict:
    markets = [{"ticker": f"{prefix}-T{i}", "status": "open"} for i in range(n_markets)]
    return {"events": [{"markets": markets}]}


@pytest.mark.asyncio
async def test_discovery_sequential_with_pause(service):
    """
    Con 2 prefixes, asyncio.sleep(2.0) debe llamarse exactamente 2 veces
    (una después de cada prefix).
    """
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.list_events = AsyncMock(
        side_effect=[
            _make_events_resp("KXMLB"),
            _make_events_resp("KXNBA"),
        ]
    )

    with patch("src.strategies.data_capture.KalshiRestClient", return_value=mock_client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXNBA"]
    ), patch("asyncio.sleep", side_effect=fake_sleep):
        await service._discover_markets()

    assert sleep_calls == [2.0, 2.0]
    assert len(service._tracked_tickers) == 4  # 2 markets × 2 prefixes


@pytest.mark.asyncio
async def test_discovery_failure_per_prefix_continues(service):
    """
    Si el primer prefix falla, el loop continúa al siguiente
    y el segundo prefix se descubre correctamente.
    """
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.list_events = AsyncMock(
        side_effect=[
            Exception("timeout on KXMLB"),
            _make_events_resp("KXNBA", n_markets=3),
        ]
    )

    with patch("src.strategies.data_capture.KalshiRestClient", return_value=mock_client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXNBA"]
    ), patch("asyncio.sleep", side_effect=fake_sleep):
        await service._discover_markets()

    # KXMLB falló → 0 markets; KXNBA exitoso → 3 markets
    assert len(service._tracked_tickers) == 3
    # sleep se llama igualmente después del prefix fallido
    assert len(sleep_calls) == 2
