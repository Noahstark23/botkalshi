"""
Tests para DataCaptureService._discover_markets.

Verifica:
- asyncio.sleep(2.0) se llama después de cada get_event y cada prefix (anti-burst)
- Un prefix fallido no aborta los demás (loop continúa)
- get_event se llama por cada event_ticker para obtener los markets
  (list_events solo retorna metadatos del evento, sin markets[])
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

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


def _make_events_resp(prefix: str, n_events: int = 1) -> dict:
    """list_events retorna solo metadatos del evento, SIN markets[]."""
    events = [{"event_ticker": f"{prefix}-E{i}"} for i in range(n_events)]
    return {"events": events}


def _make_event_detail(event_ticker: str, n_markets: int = 2) -> dict:
    """get_event retorna {"event": {...}, "markets": [...]} con markets en la raíz."""
    markets = [
        {"ticker": f"{event_ticker}-T{i}", "status": "active"} for i in range(n_markets)
    ]
    return {"event": {"event_ticker": event_ticker}, "markets": markets}


@pytest.mark.asyncio
async def test_discovery_sequential_with_pause(service):
    """
    Con 2 prefixes (cada uno con 1 evento y 2 markets):
    - sleep(2.0) antes de cada get_event (2 veces)
    - sleep(2.0) al final de cada prefix (2 veces)
    Total 4 sleep calls. Se acumulan 4 tickers (2 markets × 2 events).
    """
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.list_events = AsyncMock(
        side_effect=[
            _make_events_resp("KXMLB"),  # 1 evento: KXMLB-E0
            _make_events_resp("KXNBA"),  # 1 evento: KXNBA-E0
        ]
    )
    mock_client.get_event = AsyncMock(
        side_effect=[
            _make_event_detail("KXMLB-E0", n_markets=2),
            _make_event_detail("KXNBA-E0", n_markets=2),
        ]
    )

    with patch("src.strategies.data_capture.KalshiRestClient", return_value=mock_client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXNBA"]
    ), patch("asyncio.sleep", side_effect=fake_sleep):
        await service._discover_markets()

    # 2 sleep pre-get_event + 2 sleep post-prefix = 4 total
    assert len(sleep_calls) == 4
    assert all(s == 2.0 for s in sleep_calls)
    assert len(service._tracked_tickers) == 4  # 2 markets × 2 events


@pytest.mark.asyncio
async def test_discovery_failure_per_prefix_continues(service):
    """
    Si el primer prefix falla en list_events, el loop continúa al siguiente
    y el segundo prefix se descubre correctamente con sus 3 markets.
    """
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.list_events = AsyncMock(
        side_effect=[
            Exception("timeout on KXMLB"),  # KXMLB falla
            _make_events_resp("KXNBA"),     # KXNBA ok con 1 evento
        ]
    )
    mock_client.get_event = AsyncMock(
        return_value=_make_event_detail("KXNBA-E0", n_markets=3)
    )

    with patch("src.strategies.data_capture.KalshiRestClient", return_value=mock_client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXNBA"]
    ), patch("asyncio.sleep", side_effect=fake_sleep):
        await service._discover_markets()

    # KXMLB falló → 0 markets; KXNBA exitoso → 3 markets
    assert len(service._tracked_tickers) == 3
    # sleep: post-KXMLB(prefix) + pre-get_event(KXNBA-E0) + post-KXNBA(prefix) = 3
    assert len(sleep_calls) == 3
