"""
Motor3Engine (Motor 3, FASE 4) — ensambla poller + detector + executor en UN bucle
supervisado (60s). Lección 7: nunca gather(return_exceptions); supervisor explícito que
registra el fallo de tick y SIGUE.

Gating (dos capas, patrón Motor 2):
  - MOTOR_3_CLV_ENABLED (runner): si el engine CORRE (poll + detect + shadow-log).
  - TRADING_ENABLED: si construye el executor (CAPA A). En shadow → executor None → detecta
    y loguea las salidas CLV pero NUNCA vende (clave: el sell NO lo frena Capa C).
"""

from __future__ import annotations

import asyncio
import contextlib

from loguru import logger
from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.monitoring.health import BotState
from src.storage.models import PortfolioPosition, _naive_utc_now, get_session
from src.strategies.motor_3_clv.detector import detect_and_log, summarize_exits
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor
from src.strategies.motor_3_clv.poller import PortfolioPoller


class Motor3Engine:
    """Bucle CLV: sincroniza cartera → detecta salidas a T-30min → (liquida si trading on)."""

    LOOP_INTERVAL_SEC = 60.0

    def __init__(self, *, trading_enabled: bool = False) -> None:
        self._poller = PortfolioPoller()
        self._trading_enabled = trading_enabled
        self._executor: Motor3ExitExecutor | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(f"motor3.engine started (trading_enabled={self._trading_enabled})")
        if self._trading_enabled:
            # CAPA A: el executor (que PUEDE vender) solo existe con trading on. Cliente
            # persistente para el path de ejecución, vivo lo que dure el loop.
            async with KalshiRestClient() as client:
                self._executor = Motor3ExitExecutor(client)
                await self._loop(stop_event)
        else:
            await self._loop(stop_event)  # shadow: detecta + loguea, jamás vende

    async def _loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:
                logger.exception("motor3.engine.tick_failed")
                BotState.record_error(f"motor3.engine: {type(exc).__name__}: {exc}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self.LOOP_INTERVAL_SEC)

    async def _tick(self) -> None:
        """Un ciclo: refresca cartera, detecta salidas CLV, y liquida si hay executor."""
        await self._poller.sync_once()
        now = _naive_utc_now()
        with get_session() as s:
            positions = list(s.exec(select(PortfolioPosition)))
        # DIAG: por qué dispara (o no) este tick — evita el silencio total cuando nada es debido.
        logger.info(f"[MOTOR 3 DIAG] {summarize_exits(positions, now).one_line()}")
        due = detect_and_log(positions, now)  # SHADOW: siempre loguea las salidas debidas
        if self._executor is not None:
            for position in due:
                await self._executor.exit_position(position)
