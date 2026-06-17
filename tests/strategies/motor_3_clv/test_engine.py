"""
Tests del Motor3Engine (Motor 3) — FASE 1 QA: auditoría de Capa A (shadow vs ejecución).

Verifica el invariante de seguridad: con TRADING_ENABLED=false NO se construye cliente
REST de órdenes ni executor, pero el detector SÍ loguea el trigger (shadow). Con
TRADING_ENABLED=true sí se construye el executor (Capa A).
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger
from sqlmodel import select

from src.storage.models import PortfolioPosition, get_session
from src.strategies.motor_3_clv.engine import Motor3Engine


def _due_pos(ticker: str = "KXDUE", count: int = 10) -> PortfolioPosition:
    """Posición en la ventana de salida (close_time ~29 min en el futuro, naive UTC)."""
    close = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=29)
    return PortfolioPosition(ticker=ticker, side="yes", count=count, close_time=close)


@contextmanager
def _fake_session(positions: list[PortfolioPosition]):
    s = MagicMock()
    s.exec.return_value = positions
    yield s


def _patch_db(positions: list[PortfolioPosition]):
    return patch(
        "src.strategies.motor_3_clv.engine.get_session", new=lambda: _fake_session(positions)
    )


@pytest.mark.asyncio
async def test_capa_a_no_rest_client_nor_executor_when_trading_disabled():
    """TRADING_ENABLED=false → NO se construye KalshiRestClient (órdenes) ni executor."""
    stop = asyncio.Event()
    stop.set()  # salir del loop inmediatamente
    eng = Motor3Engine(trading_enabled=False)
    with patch("src.strategies.motor_3_clv.engine.KalshiRestClient") as mock_client:
        await eng.run(stop)
    assert eng._executor is None
    mock_client.assert_not_called()  # jamás se instanció el cliente de órdenes


@pytest.mark.asyncio
async def test_capa_a_builds_executor_when_trading_enabled():
    """TRADING_ENABLED=true → SÍ se construye el executor (Capa A) con su cliente REST."""
    stop = asyncio.Event()
    stop.set()
    eng = Motor3Engine(trading_enabled=True)
    fake_client = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "src.strategies.motor_3_clv.engine.KalshiRestClient", return_value=ctx
    ) as mock_client:
        await eng.run(stop)
    mock_client.assert_called_once()  # se instanció el cliente de órdenes
    assert eng._executor is not None  # executor construido (Capa A)


@pytest.mark.asyncio
async def test_shadow_tick_logs_detection_but_never_sells():
    """Shadow (executor None): el detector LOGUEA el trigger pero no se ejecuta venta."""
    eng = Motor3Engine(trading_enabled=False)
    eng._poller.sync_once = AsyncMock(return_value=1)  # no clobber DB
    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        with _patch_db([_due_pos("KXSHADOW")]):
            await eng._tick()
    finally:
        logger.remove(sink)
    assert any("[MOTOR 3 SHADOW] CLV Exit" in m and "KXSHADOW" in m for m in captured)
    assert eng._executor is None  # nunca hubo executor → imposible vender


@pytest.mark.asyncio
async def test_partial_fill_reattempts_remainder_next_tick():
    """
    FASE 3 (state loop): tras un fill parcial (10→6), el SIGUIENTE tick re-detecta (sigue
    en ventana) y reintenta el remanente. Se simula el re-sync del poller actualizando la
    PortfolioPosition a 6 tras la primera venta.
    """
    close = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=29)
    with get_session() as s:
        s.add(PortfolioPosition(ticker="KXP", side="yes", count=10, close_time=close))
        s.commit()

    eng = Motor3Engine(trading_enabled=False)
    eng._poller.sync_once = AsyncMock()  # el side-effect del executor simula el re-sync
    seen_counts: list[int] = []

    async def _fake_exit(position):
        seen_counts.append(position.count)
        if (
            position.count == 10
        ):  # primer intento: vende 4 → quedan 6 (lo que sincronizaría el poller)
            with get_session() as s:
                row = s.exec(
                    select(PortfolioPosition).where(PortfolioPosition.ticker == "KXP")
                ).first()
                row.count = 6
                s.add(row)
                s.commit()
        return MagicMock()

    eng._executor = MagicMock()
    eng._executor.exit_position = AsyncMock(side_effect=_fake_exit)

    await eng._tick()  # vende parcial → posición queda en 6
    await eng._tick()  # reintenta el remanente
    assert seen_counts == [10, 6]  # segundo intento sobre los 6 restantes
