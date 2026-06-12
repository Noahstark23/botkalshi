"""
Tests de DataCaptureService._discover_markets (P2: por prefijo) + re-discovery.

Verifica:
- filtro por PREFIJO real (familias enteras KXWC*/KXFIFA*; series ajenas afuera);
- paginación con cursor + pacing entre páginas;
- corte de seguridad DISCOVERY_MAX_PAGES;
- devuelve SOLO los tickers nuevos (re-discovery delta);
- _run_rediscovery suscribe los nuevos EN CALIENTE y sobrevive fallos (best-effort).
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


def _markets_page(tickers: list[str], cursor: str | None = None) -> dict:
    return {"markets": [{"ticker": t} for t in tickers], "cursor": cursor}


def _mock_client(pages: list[dict]) -> AsyncMock:
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.list_markets = AsyncMock(side_effect=pages)
    return client


@pytest.mark.asyncio
async def test_discovery_filters_by_prefix_covering_families(service):
    """KXWC*/KXFIFA* enteras entran por prefijo; series ajenas (KXBTC) quedan afuera."""
    page = _markets_page([
        "KXFIFAGAME-26JUN12-ARG-MEX",   # el match market que el listado exacto perdía
        "KXWCGROUPWIN-26-A",
        "KXWCNEWSERIES-26-X",            # serie NUEVA de la familia → entra sola
        "KXNBA-26-LAL",
        "KXBTC-26DEC31",                 # ajena → afuera
    ])
    client = _mock_client([page])

    with patch("src.strategies.data_capture.KalshiRestClient", return_value=client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXWC", "KXFIFA", "KXNBA"]
    ):
        new = await service._discover_markets()

    assert new == {
        "KXFIFAGAME-26JUN12-ARG-MEX",
        "KXWCGROUPWIN-26-A",
        "KXWCNEWSERIES-26-X",
        "KXNBA-26-LAL",
    }
    assert "KXBTC-26DEC31" not in service._tracked_tickers
    assert client.list_markets.await_count == 1


@pytest.mark.asyncio
async def test_discovery_paginates_with_cursor_and_pacing(service):
    """Sigue el cursor hasta la última página, con pacing entre páginas."""
    sleep_calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    pages = [
        _markets_page(["KXNBA-1"], cursor="c1"),
        _markets_page(["KXNBA-2"], cursor="c2"),
        _markets_page(["KXNBA-3"], cursor=None),  # última
    ]
    client = _mock_client(pages)

    with patch("src.strategies.data_capture.KalshiRestClient", return_value=client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXNBA"]
    ), patch("asyncio.sleep", side_effect=fake_sleep):
        new = await service._discover_markets()

    assert new == {"KXNBA-1", "KXNBA-2", "KXNBA-3"}
    assert client.list_markets.await_count == 3
    # El cursor se encadenó correctamente.
    cursors = [c.kwargs.get("cursor") for c in client.list_markets.await_args_list]
    assert cursors == [None, "c1", "c2"]
    # Pacing entre páginas (no después de la última).
    assert sleep_calls == [service.DISCOVERY_PAGE_PACING_SEC] * 2


@pytest.mark.asyncio
async def test_discovery_safety_cap_stops_runaway_pagination(service):
    """Cursor que nunca termina → corta en DISCOVERY_MAX_PAGES (sin loop infinito)."""
    service.DISCOVERY_MAX_PAGES = 3
    endless = AsyncMock()
    endless.__aenter__ = AsyncMock(return_value=endless)
    endless.__aexit__ = AsyncMock(return_value=False)
    endless.list_markets = AsyncMock(return_value=_markets_page(["KXNBA-X"], cursor="more"))

    with patch("src.strategies.data_capture.KalshiRestClient", return_value=endless), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXNBA"]
    ), patch("asyncio.sleep", new=AsyncMock()):
        await service._discover_markets()

    assert endless.list_markets.await_count == 3  # exactamente el cap


@pytest.mark.asyncio
async def test_discovery_returns_only_new_tickers(service):
    """El delta: lo ya trackeado no se reporta como nuevo (insumo del re-discovery)."""
    service._tracked_tickers = {"KXNBA-VIEJO"}
    client = _mock_client([_markets_page(["KXNBA-VIEJO", "KXNBA-NUEVO"])])

    with patch("src.strategies.data_capture.KalshiRestClient", return_value=client), patch(
        "src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXNBA"]
    ):
        new = await service._discover_markets()

    assert new == {"KXNBA-NUEVO"}
    assert service._tracked_tickers == {"KXNBA-VIEJO", "KXNBA-NUEVO"}


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
