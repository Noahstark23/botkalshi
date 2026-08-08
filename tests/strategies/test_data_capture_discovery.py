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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.data_capture import DataCaptureService, discovery_series


@pytest.fixture
def service():
    with (
        patch("src.strategies.data_capture.get_settings"),
        patch("src.strategies.data_capture.KalshiWebSocket"),
    ):
        svc = DataCaptureService()
        svc._tracked_tickers = set()
        return svc


def _events_resp(prefix: str, n_events: int = 1) -> dict:
    return {"events": [{"event_ticker": f"{prefix}-E{i}"} for i in range(n_events)]}


def _event_detail(
    event_ticker: str,
    n_markets: int = 2,
    status: str = "active",
    open_time: str | None = None,
) -> dict:
    markets = [
        {"ticker": f"{event_ticker}-T{i}", "status": status, "open_time": open_time}
        for i in range(n_markets)
    ]
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
    with (
        patch("src.strategies.data_capture.KalshiRestClient", return_value=client),
        patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXFIFAGAME"]),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
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
    with (
        patch("src.strategies.data_capture.KalshiRestClient", return_value=client),
        patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXNBA"]),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        new = await service._discover_markets()

    assert len(new) == 3  # KXMLB falló → 0; KXNBA → 3


@pytest.mark.asyncio
async def test_discovery_returns_only_new_tickers(service):
    """Delta para el re-discovery: lo ya trackeado no se reporta como nuevo."""
    service._tracked_tickers = {"KXNBA-E0-T0"}
    client = _client([_events_resp("KXNBA")], _event_detail("KXNBA-E0", 2))
    with (
        patch("src.strategies.data_capture.KalshiRestClient", return_value=client),
        patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXNBA"]),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        new = await service._discover_markets()

    assert new == {"KXNBA-E0-T1"}
    assert service._tracked_tickers == {"KXNBA-E0-T0", "KXNBA-E0-T1"}


@pytest.mark.asyncio
async def test_discovery_skips_closed_markets(service):
    """Markets con status fuera de open/active no se trackean."""
    client = _client([_events_resp("KXNBA")], _event_detail("KXNBA-E0", 2, status="settled"))
    with (
        patch("src.strategies.data_capture.KalshiRestClient", return_value=client),
        patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXNBA"]),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        new = await service._discover_markets()
    assert new == set()


# ── Metadata para la purga de recovery de V2 (incidente 2026-07-21) ──────────────


@pytest.mark.asyncio
async def test_discovery_collects_metadata_and_feeds_v2_manager(service):
    """open_time + status llegan al v2_manager vía set_market_metadata (además del
    set_close_times legado): el insumo de la purga de futuros sin abrir."""
    service._v2_manager = MagicMock()
    client = _client(
        [_events_resp("KXMLB")],
        _event_detail("KXMLB-E0", 1, status="active", open_time="2099-01-01T00:00:00Z"),
    )
    with (
        patch("src.strategies.data_capture.KalshiRestClient", return_value=client),
        patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB"]),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await service._discover_markets()

    assert service._market_meta["KXMLB-E0-T0"] == {
        "open_time": "2099-01-01T00:00:00Z",
        "status": "active",
    }
    service._v2_manager.set_market_metadata.assert_called_once_with(service._market_meta)
    service._v2_manager.set_close_times.assert_called_once()


@pytest.mark.asyncio
async def test_rediscovery_updates_metadata_of_transitioned_tracked_ticker(service):
    """Un ticker YA trackeado que transicionó fuera de active/open actualiza su metadata
    (antes se salteaba y el status quedaba rancio en 'active' → la recovery jamás lo purgaba)."""
    service._tracked_tickers = {"KXNBA-E0-T0"}
    client = _client([_events_resp("KXNBA")], _event_detail("KXNBA-E0", 1, status="settled"))
    with (
        patch("src.strategies.data_capture.KalshiRestClient", return_value=client),
        patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXNBA"]),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        new = await service._discover_markets()

    assert new == set()  # no se trackea nada nuevo
    assert service._market_meta["KXNBA-E0-T0"]["status"] == "settled"  # pero la meta se refresca


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

    with (
        patch.object(service, "_discover_markets", side_effect=flaky_discover),
        patch("src.strategies.data_capture.BotState") as mock_state,
    ):
        await asyncio.wait_for(service._run_rediscovery(), timeout=1.0)

    assert calls["n"] == 2  # sobrevivió el fallo y reintentó
    mock_state.record_error.assert_called()


# =====================================================
# discovery_series(): la lista base + DISCOVERY_EXTRA_SERIES (env)
# =====================================================
# Punto ciego 2026-08-08: MOTOR2_SERIES filtra SOBRE lo trackeado — no descubre nada.
# El experimento NFL llevaba KXNFLGAME en MOTOR2_SERIES pero el discovery solo conocía
# KXNFL (futuro de campeón): el lado Kalshi corría ciego. Las series GAME nuevas
# entran por env, sin tocar código.


def _settings_con_extras(extras: str) -> MagicMock:
    s = MagicMock()
    s.DISCOVERY_EXTRA_SERIES = extras
    return s


def test_discovery_series_default_vacio_es_solo_la_base():
    with patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXMLBGAME"]):
        assert discovery_series(_settings_con_extras("")) == ["KXMLB", "KXMLBGAME"]


def test_discovery_series_extras_se_agregan_al_final():
    with patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB"]):
        assert discovery_series(_settings_con_extras(" KXNFLGAME , KXEPLGAME ")) == [
            "KXMLB",
            "KXNFLGAME",
            "KXEPLGAME",
        ]


def test_discovery_series_dedup_contra_la_base():
    """CONTROL: repetir una serie de la base en el env no la duplica (dos pasadas de
    list_events sobre la misma serie serían el doble de requests y de pausas)."""
    with patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB", "KXMLBGAME"]):
        assert discovery_series(_settings_con_extras("KXMLBGAME,KXNFLGAME")) == [
            "KXMLB",
            "KXMLBGAME",
            "KXNFLGAME",
        ]


def test_discovery_series_settings_mockeado_degrada_a_base():
    """FAIL-SAFE de lectura: un settings sin el campo como string (mock del harness,
    env corrupto) NO rompe el discovery — degrada a la lista base."""
    with patch("src.strategies.data_capture.TARGET_SERIES_PREFIXES", ["KXMLB"]):
        assert discovery_series(MagicMock()) == ["KXMLB"]
