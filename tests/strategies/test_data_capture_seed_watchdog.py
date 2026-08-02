"""
Watchdog de siembra de books (P0 2026-08-02) — wiring en DataCaptureService.

Pinea el CALL SITE (regla 6 de desarrollo-bot): el loop llama seed_blind_sids() del
manager V2, sobrevive errores por tick (Lección 7), y DESHABILITADO no completa la
task (participa del FIRST_COMPLETED de run(): completarse tumbaría la captura).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from src.monitoring.health import BotState
from src.strategies.data_capture import DataCaptureService


def _service(interval: float) -> DataCaptureService:
    with (
        patch("src.strategies.data_capture.get_settings") as gs,
        patch("src.strategies.data_capture.KalshiWebSocket"),
    ):
        gs.return_value.ORDERBOOK_V2_SEED_WATCHDOG_INTERVAL_SEC = interval
        return DataCaptureService()


async def test_watchdog_llama_seed_blind_sids_del_manager():
    svc = _service(interval=0.01)
    svc._v2_manager = AsyncMock()

    task = asyncio.create_task(svc._run_seed_watchdog())
    await asyncio.sleep(0.08)
    svc._stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert svc._v2_manager.seed_blind_sids.await_count >= 1


async def test_watchdog_sobrevive_errores_por_tick():
    """Lección 7: un tick que explota se registra y el loop SIGUE."""
    BotState.last_error = None
    svc = _service(interval=0.01)
    svc._v2_manager = AsyncMock()
    svc._v2_manager.seed_blind_sids.side_effect = RuntimeError("boom")

    task = asyncio.create_task(svc._run_seed_watchdog())
    await asyncio.sleep(0.08)
    assert not task.done()  # el loop sigue vivo tras varios ticks fallidos
    svc._stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert svc._v2_manager.seed_blind_sids.await_count >= 2
    assert BotState.last_error is not None and "Seed watchdog" in BotState.last_error
    BotState.last_error = None
    BotState.last_error_at = None


async def test_watchdog_deshabilitado_no_completa_la_task():
    """interval=0: la task espera el stop_event en vez de retornar — completarse
    dispararía el FIRST_COMPLETED de run() y tumbaría la captura entera."""
    svc = _service(interval=0.0)
    task = asyncio.create_task(svc._run_seed_watchdog())
    await asyncio.sleep(0.05)

    assert not task.done()  # NO completó sola

    svc._stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)


async def test_watchdog_sin_manager_no_explota():
    svc = _service(interval=0.01)
    assert svc._v2_manager is None

    task = asyncio.create_task(svc._run_seed_watchdog())
    await asyncio.sleep(0.04)
    assert not task.done()
    svc._stop_event.set()
    await asyncio.wait_for(task, timeout=1.0)
