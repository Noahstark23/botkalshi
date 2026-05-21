"""Tests del supervisor pattern y health checks — excepciones reportadas correctamente.

Regression tests para el bug del 14 mayo 2026: asyncio.gather(return_exceptions=True)
tragaba TypeError de ws.run(), dejando BotState.last_error=null durante 11h.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.monitoring.health import BotState
from src.strategies.data_capture import DataCaptureService


@pytest.fixture
def service():
    with patch("src.strategies.data_capture.get_settings"), patch(
        "src.strategies.data_capture.KalshiWebSocket"
    ):
        svc = DataCaptureService()
        svc._tracked_tickers = set()
        svc.ws = MagicMock()
        return svc


@pytest.fixture(autouse=True)
def reset_botstate():
    BotState.last_error = None
    BotState.last_error_at = None
    BotState.ws_connected = False
    BotState.capture_running = False
    BotState.started_at = datetime.now(UTC)
    yield
    BotState.last_error = None
    BotState.ws_connected = False
    BotState.capture_running = False


@pytest.mark.asyncio
async def test_ws_supervisor_records_typerror_when_lib_incompatible(service):
    """
    Regression: si ws.run() levanta TypeError (caso real del 14 mayo),
    el supervisor debe reportar a BotState.record_error y re-levar.
    """
    service.ws.run = AsyncMock(
        side_effect=TypeError(
            "BaseEventLoop.create_connection() got an unexpected keyword argument 'extra_headers'"
        )
    )

    with pytest.raises(TypeError):
        await service._run_ws_supervised()

    assert BotState.last_error is not None
    assert "TypeError" in BotState.last_error or "extra_headers" in BotState.last_error


@pytest.mark.asyncio
async def test_ws_supervisor_records_generic_exception(service):
    """Cualquier excepción de ws.run() debe llegar a BotState."""
    service.ws.run = AsyncMock(side_effect=ConnectionError("network gone"))

    with pytest.raises(ConnectionError):
        await service._run_ws_supervised()

    assert BotState.last_error is not None
    assert "ConnectionError" in BotState.last_error


@pytest.mark.asyncio
async def test_ws_supervisor_propagates_cancelled_error(service):
    """CancelledError no debe reportarse como error, solo propagarse."""
    service.ws.run = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await service._run_ws_supervised()

    assert BotState.last_error is None


@pytest.mark.asyncio
async def test_snapshots_supervisor_records_error_on_exception(service):
    """Excepción en _periodic_snapshots debe llegar a BotState."""

    async def boom():
        raise ValueError("snapshot boom")

    service._periodic_snapshots = boom

    with pytest.raises(ValueError):
        await service._run_snapshots_supervised()

    assert BotState.last_error is not None
    assert "ValueError" in BotState.last_error


@pytest.mark.asyncio
async def test_snapshots_supervisor_propagates_cancelled_error(service):
    """CancelledError en snapshots no debe reportarse como error."""

    async def cancel():
        raise asyncio.CancelledError()

    service._periodic_snapshots = cancel

    with pytest.raises(asyncio.CancelledError):
        await service._run_snapshots_supervised()

    assert BotState.last_error is None


@pytest.mark.asyncio
async def test_check_ws_health_detects_zombie(service):
    """WS conectado pero sin tráfico > 5min → record_error con 'zombie'."""
    service.ws.is_connected = True
    service.ws.last_message_at = datetime.now(UTC) - timedelta(seconds=400)

    await service._check_ws_health()

    assert BotState.last_error is not None
    assert "zombie" in BotState.last_error.lower()


@pytest.mark.asyncio
async def test_check_ws_health_no_connection_after_grace(service):
    """WS no conectado después de 2min de uptime → record_error."""
    service.ws.is_connected = False
    BotState.started_at = datetime.now(UTC) - timedelta(seconds=180)

    await service._check_ws_health()

    assert BotState.last_error is not None
    assert "not connected" in BotState.last_error.lower()


@pytest.mark.asyncio
async def test_check_ws_health_no_error_within_grace_period(service):
    """WS no conectado pero < 2min de uptime → no record_error."""
    service.ws.is_connected = False
    BotState.started_at = datetime.now(UTC) - timedelta(seconds=60)

    await service._check_ws_health()

    assert BotState.last_error is None


@pytest.mark.asyncio
async def test_check_ws_health_sets_ws_connected_true_when_active(service):
    """WS conectado con mensajes recientes → ws_connected=True, no error."""
    service.ws.is_connected = True
    service.ws.last_message_at = datetime.now(UTC) - timedelta(seconds=30)

    await service._check_ws_health()

    assert BotState.ws_connected is True
    assert BotState.last_error is None


@pytest.mark.asyncio
async def test_check_ws_health_sets_ws_connected_false_when_down(service):
    """WS not connected → ws_connected=False."""
    service.ws.is_connected = False
    BotState.started_at = datetime.now(UTC)  # dentro del grace period

    await service._check_ws_health()

    assert BotState.ws_connected is False


@pytest.fixture
def loguru_caplog(caplog):
    """Redirect Loguru output to pytest's caplog handler."""
    from loguru import logger
    handler_id = logger.add(caplog.handler, format="{message}", level="DEBUG")
    yield caplog
    logger.remove(handler_id)


@pytest.mark.asyncio
async def test_zombie_detection_emits_warning_log(service, loguru_caplog):
    """Cuando se detecta zombie, debe emitirse log WARNING con los tres campos esperados."""
    service.ws.is_connected = True
    service.ws.last_message_at = datetime.now(UTC) - timedelta(seconds=400)

    await service._check_ws_health()

    log_text = loguru_caplog.text
    assert "ws.zombie.detected" in log_text
    assert "silence_seconds=" in log_text
    assert "ws_is_connected=" in log_text
    assert "action_taken=record_error" in log_text
