"""
Production runner.

Orquesta:
    1. Health server FastAPI
    2. Data capture service
    3. (Futuro) Strategy engines

Maneja:
    - Shutdown limpio en SIGTERM (Coolify lo envía al re-deploy)
    - Logging de eventos importantes
    - Telegram alerts opcionales
    - Tracking en bot_runs table

Punto de entrada del container Docker.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from datetime import UTC, datetime

import uvicorn
from loguru import logger

from src.monitoring.health import BotState, app
from src.monitoring.telegram_alerts import alert_shutdown, alert_startup
from src.storage.models import BotRun, get_session, init_db
from src.strategies.data_capture import DataCaptureService
from src.utils.config import get_settings
from src.utils.logging import setup_logging


class ProductionRunner:
    """Orquestador principal del bot en producción."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._stop_event = asyncio.Event()
        self._capture: DataCaptureService | None = None
        self._uvicorn_server: uvicorn.Server | None = None
        self._bot_run_id: int | None = None

    # =====================================================
    # DB tracking
    # =====================================================

    def _record_run_start(self) -> None:
        """Registra arranque en DB para auditoría."""
        motors_enabled = []
        if self.settings.MOTOR_1_ARBITRAGE_ENABLED:
            motors_enabled.append("motor_1_arbitrage")
        if self.settings.MOTOR_2_SPORTSBOOK_ENABLED:
            motors_enabled.append("motor_2_sportsbook")
        if self.settings.MOTOR_3_CLV_ENABLED:
            motors_enabled.append("motor_3_clv")

        with get_session() as s:
            run = BotRun(
                environment=self.settings.KALSHI_ENV,
                trading_enabled=self.settings.TRADING_ENABLED,
                motors_enabled=json.dumps(motors_enabled),
                capital_at_start=self.settings.ACTIVE_CAPITAL_USD,
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            self._bot_run_id = run.id

    def _record_run_end(self, crash_reason: str | None = None) -> None:
        """Marca fin del run en DB."""
        if self._bot_run_id is None:
            return
        try:
            with get_session() as s:
                run = s.get(BotRun, self._bot_run_id)
                if run:
                    run.ended_at = datetime.now(UTC)
                    run.crash_reason = crash_reason
                    s.add(run)
                    s.commit()
        except Exception:
            logger.exception("No se pudo registrar fin de run")

    # =====================================================
    # Servicios
    # =====================================================

    async def _run_health_server(self) -> None:
        """Health server FastAPI con uvicorn."""
        config = uvicorn.Config(
            app,
            host=self.settings.HEALTH_HOST,
            port=self.settings.HEALTH_PORT,
            log_level="warning",
            access_log=False,
            loop="asyncio",
        )
        self._uvicorn_server = uvicorn.Server(config)
        logger.info(f"Health server: http://{self.settings.HEALTH_HOST}:{self.settings.HEALTH_PORT}")
        await self._uvicorn_server.serve()

    async def _run_data_capture(self) -> None:
        """Data capture service."""
        # Pequeño delay para que health server esté listo primero
        await asyncio.sleep(2)

        self._capture = DataCaptureService()
        await self._capture.run()

    # =====================================================
    # Lifecycle
    # =====================================================

    async def shutdown(self, reason: str = "signal") -> None:
        """Shutdown limpio. Idempotente."""
        if self._stop_event.is_set():
            return

        logger.warning(f"🛑 Shutdown iniciado: {reason}")
        self._stop_event.set()

        BotState.is_paused = True
        BotState.pause_reason = f"shutdown: {reason}"

        # Detener capture
        if self._capture:
            await self._capture.stop()

        # Detener uvicorn
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True

        await alert_shutdown(reason)
        self._record_run_end(crash_reason=None if reason == "signal" else reason)

    async def run(self) -> int:
        """Punto de entrada principal. Retorna exit code."""
        try:
            # Inicialización
            logger.info(f"🚀 Bot arrancando en {self.settings.KALSHI_ENV.upper()}")
            logger.info(f"Capital activo: ${self.settings.ACTIVE_CAPITAL_USD}")
            logger.info(f"Trading enabled: {self.settings.TRADING_ENABLED}")

            init_db()
            BotState.db_initialized = True
            logger.success("DB inicializada")

            self._record_run_start()
            await alert_startup()

            # Lanzar servicios concurrentemente
            tasks = [
                asyncio.create_task(self._run_health_server(), name="health"),
                asyncio.create_task(self._run_data_capture(), name="capture"),
            ]

            # Esperar a que cualquiera termine (idealmente nunca, salvo shutdown)
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_EXCEPTION,
            )

            # Si alguna task crasheó, log y marcar
            for task in done:
                if task.exception():
                    err = f"Task {task.get_name()} crashed: {task.exception()}"
                    logger.exception(err)
                    BotState.record_error(err)

            # Cancelar pendientes
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            self._record_run_end()
            return 0

        except Exception as e:
            logger.exception("Fatal en ProductionRunner")
            self._record_run_end(crash_reason=str(e)[:500])
            return 1


# =====================================================
# Entry point
# =====================================================


async def _main() -> int:
    setup_logging()
    runner = ProductionRunner()

    # Manejo de señales
    loop = asyncio.get_running_loop()

    def _signal_handler(sig: signal.Signals) -> None:
        logger.warning(f"Señal recibida: {sig.name}")
        asyncio.create_task(runner.shutdown(reason=sig.name))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler, sig)

    return await runner.run()


def main() -> None:
    """CLI entry point."""
    exit_code = asyncio.run(_main())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
