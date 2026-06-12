"""
Tests de DataCaptureService._discover_markets (HOTFIX: series exactas, flujo #30)
+ re-discovery periódico (#41, conservado).

El listado amplio por paginación (#41) falló en producción (>40k markets abiertos,
los deportivos fuera del alcance del cap) → el discovery interno volvió al flujo
conocido-bueno: list_events(series_ticker=X) + get_event por evento. La firma DELTA
(set de tickers nuevos) se conserva: el re-discovery la usa para suscribir en caliente.
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


def _events_resp(prefix: str, n_events: int = 1) -> dict:
    return {"events": [{"event_ticker": f"{prefix}-E{i}"} for i in range(n_events)]}


def _event_detail(event_ticker: str, n_markets: int = 2, status: str = "active") -> dict:
    markets = [{"ticker": f"{event_ticker}-T{i}", "status": status} for i in range(n_markets)]
    return {"event": {"event_ticker": event_ticker}, "markets": markets}


def _client(list_events_side: list, get_event_side) -> AsyncMock:
    c = AsyncMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.list_events = AsyncMock(side_effect=list_events_side)
    if isinstance(get_event_side, list):
        c.get_event = AsyncMock(side_effect=get_event_side)
    else:
        c.get_event = AsyncMock(return_value=get_event_side)
    return c


@pytest.mark.asyncio
async def test_discovery_exact_series_collects_open_markets(service):
    """Flujo #30: por serie exacta → eventos → get_event → markets open/active; delta correcto."""
    client = _client(
        [_events_resp("KXMLB"), _events_resp("KXFIFAGAME")],
        [_event_detail("KXMLB-E0", 2), _event_detail("KXFIFAGAME-E0", 3)],
    )
    with patch("src.strategies.data_capture.KalshiRestClient", return_value=client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXFIFAGAME"]
    ), patch("asyncio.sleep", new=AsyncMock()):
        new = await service._discover_markets()

    assert len(new) == 5  # 2 + 3, todos nuevos
    assert len(service._tracked_tickers) == 5
    # list_events recibió la serie EXACTA (no un prefijo amplio).
    series_args = [c.kwargs.get("series_ticker") for c in client.list_events.await_args_list]
    assert series_args == ["KXMLB", "KXFIFAGAME"]


@pytest.mark.asyncio
async def test_discovery_failure_per_series_continues(service):
    """Una serie que falla NO aborta las demás (resiliencia por-serie del flujo #30)."""
    client = _client(
        [Exception("timeout en KXMLB"), _events_resp("KXNBA")],
        _event_detail("KXNBA-E0", 3),
    )
    with patch("src.strategies.data_capture.KalshiRestClient", return_value=client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXNBA"]
    ), patch("asyncio.sleep", new=AsyncMock()):
        new = await service._discover_markets()

    assert len(new) == 3  # KXMLB falló → 0; KXNBA → 3


@pytest.mark.asyncio
async def test_discovery_returns_only_new_tickers(service):
    """Delta para el re-discovery: lo ya trackeado no se reporta como nuevo."""
    service._tracked_tickers = {"KXNBA-E0-T0"}
    client = _client([_events_resp("KXNBA")], _event_detail("KXNBA-E0", 2))
    with patch("src.strategies.data_capture.KalshiRestClient", return_value=client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXNBA"]
    ), patch("asyncio.sleep", new=AsyncMock()):
        new = await service._discover_markets()

    assert new == {"KXNBA-E0-T1"}
    assert service._tracked_tickers == {"KXNBA-E0-T0", "KXNBA-E0-T1"}


@pytest.mark.asyncio
async def test_discovery_skips_closed_markets(service):
    """Markets con status fuera de open/active no se trackean."""
    client = _client([_events_resp("KXNBA")], _event_detail("KXNBA-E0", 2, status="settled"))
    with patch("src.strategies.data_capture.KalshiRestClient", return_value=client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXNBA"]
    ), patch("asyncio.sleep", new=AsyncMock()):
        new = await service._discover_markets()
    assert new == set()


# ── Re-discovery (#41, se conserva: independiente del método interno) ─────────────


@pytest.mark.asyncio
async def test_rediscovery_subscribes_new_tickers_hot(service):
    """Un ciclo de re-discovery suscribe los nuevos en caliente vía queue_subscription."""
    service.REDISCOVERY_INTERVAL_SEC = 0.01

    async def fake_discover() -> set[str]:
        service._stop_event.set()  # un solo ciclo
        return {"KXFIFAGAME-NEW1", "KXFIFAGAME-NEW2"}

    with patch.object(service, "_discover_markets", side_effect=fake_discover):
        await asyncio.wait_for(service._run_rediscovery(), timeout=1.0)

    subs = service.ws.queue_subscription.call_args_list
    assert len(subs) == 1
    assert sorted(subs[0].kwargs["market_tickers"]) == ["KXFIFAGAME-NEW1", "KXFIFAGAME-NEW2"]
    assert subs[0].kwargs["channels"] == ["orderbook_delta", "ticker"]


@pytest.mark.asyncio
async def test_rediscovery_survives_failures_best_effort(service):
    """Un ciclo que falla NO mata el loop: registra el error y sigue al siguiente."""
    service.REDISCOVERY_INTERVAL_SEC = 0.01
    calls = {"n": 0}

    async def flaky_discover() -> set[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("API caída")
        service._stop_event.set()
        return set()

    with patch.object(service, "_discover_markets", side_effect=flaky_discover), patch(
        "src.strategies.data_capture.BotState"
    ) as mock_state:
        await asyncio.wait_for(service._run_rediscovery(), timeout=1.0)

    assert calls["n"] == 2  # sobrevivió el fallo y reintentó
    mock_state.record_error.assert_called()
