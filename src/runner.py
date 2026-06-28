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
import signal
import sys
from datetime import UTC, datetime

import uvicorn
from loguru import logger

from src.clients.kalshi_rest import KalshiRestClient
from src.monitoring.health import BotState, app
from src.monitoring.telegram_alerts import alert_shutdown, alert_startup, send_alert
from src.risk.manager import RiskManager
from src.storage.models import BotRun, get_session, init_db, kill_switch_engaged
from src.strategies.data_capture import DataCaptureService
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor
from src.strategies.motor_rest_arb.settlement import KalshiSettlementSource, SettlementPoller
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
        logger.info(
            f"Health server: http://{self.settings.HEALTH_HOST}:{self.settings.HEALTH_PORT}"
        )
        await self._uvicorn_server.serve()

    async def _run_data_capture(self) -> None:
        """Data capture service."""
        # Pequeño delay para que health server esté listo primero
        await asyncio.sleep(2)

        # Cliente REST PERSISTENTE para el path de ejecución del Motor REST (live):
        # se instancia UNA vez, pool abierto toda la vida del servicio, inyectado al
        # servicio (y de ahí al executor en la Capa 2). Solo se abre con
        # TRADING_ENABLED=true; en shadow NO se crea → la ruta es idéntica a la actual.
        if self.settings.TRADING_ENABLED:
            async with KalshiRestClient() as rest_client:
                self._capture = DataCaptureService(rest_client=rest_client)
                await self._capture.run()
        else:
            self._capture = DataCaptureService()
            await self._capture.run()

    async def _rehydrate_kill_switch(self) -> None:
        """
        Si el kill-switch persistente quedó ENGAGED, arrancar PAUSADO + avisar.

        Tolerante a fallas de DB: si no se puede leer, NO bloquea el arranque (el
        TRADING_ENABLED gate + la Capa A siguen protegiendo); se registra el error.
        """
        try:
            engaged, reason = kill_switch_engaged()
        except Exception:
            logger.exception("No se pudo leer el kill-switch persistente al boot")
            BotState.record_error("kill_switch read failed at boot")
            return
        if not engaged:
            return
        BotState.is_paused = True
        BotState.pause_reason = f"kill-switch persistente: {reason}"
        logger.critical(f"ARRANQUE PAUSADO por kill-switch persistente no resuelto: {reason}")
        try:
            await send_alert(
                f"🚨 Bot arrancó PAUSADO: kill-switch previo sin resolver ({reason}). "
                "No tradea hasta scripts/clear_kill_switch.py + redeploy.",
                urgent=True,
            )
        except Exception:
            logger.exception("alerta de boot-paused falló")

    async def _run_settlement(self) -> None:
        """
        Poller de settlement (PR-B) — corre SIEMPRE, NO gateado por TRADING_ENABLED.

        Razón: si apago el flag para investigar un incidente, el settlement de lo YA
        tradeado debe seguir corriendo (las posiciones abiertas tienen que liquidarse
        igual). Es no-op barato cuando no hay Trade 'filled' pendientes (la query del
        poller no devuelve nada). Atómico por arb_id (heredado de SettlementPoller:
        ambas patas en una transacción o ninguna; huérfana imposible).

        Read-only contra Kalshi (get_market.result). Si el cliente/poller cae, se
        registra y la task termina — NO tira el bot (el resto sigue capturando).
        """
        await asyncio.sleep(5)  # dejar que init_db / boot terminen
        try:
            async with KalshiRestClient() as client:
                source = KalshiSettlementSource(client)
                await SettlementPoller(source).run(self._stop_event)
        except Exception as e:
            msg = f"settlement runner: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)

    async def _run_motor3_clv(self) -> None:
        """
        Motor 3 (CLV) — gateado por MOTOR_3_CLV_ENABLED. Liquida posiciones direccionales
        a T-30min del cierre para capturar el closing line value. CAPA A: el executor (que
        vende) solo se construye con TRADING_ENABLED=true — en shadow detecta y loguea las
        salidas pero NUNCA vende (clave: place_order no frena 'sell', así que la protección
        es Capa A, no el muro global). Best-effort: si cae, se registra y la task termina.
        """
        if not self.settings.MOTOR_3_CLV_ENABLED:
            return  # opt-in; default off → no-op (no toca nada en prod)
        await asyncio.sleep(20)  # dejar que init_db / boot terminen
        try:
            from src.strategies.motor_3_clv.engine import Motor3Engine

            await Motor3Engine(
                trading_enabled=(
                    self.settings.TRADING_ENABLED and self.settings.MOTOR_3_EXECUTION_ENABLED
                ),
                take_profit_enabled=self.settings.MOTOR_3_TAKE_PROFIT_ENABLED,
                tp_threshold=self.settings.MOTOR_3_TAKE_PROFIT_CENTS,
                trailing_enabled=self.settings.MOTOR_3_TRAILING_ENABLED,
                trailing_drop=self.settings.MOTOR_3_TRAILING_DROP_CENTS,
            ).run(self._stop_event)
        except Exception as e:
            msg = f"motor3_clv runner: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)

    async def _run_motor1_arb(self) -> None:
        """
        Motor 1 (arbitraje binario WS) — gateado por MOTOR_1_ARBITRAGE_ENABLED. Detecta
        cruces YES+NO sobre el OrderbookManagerV2 (que vive en data_capture, alimentado por
        el WS) y graba EdgeWindow. CAPA A: el ArbitrageExecutor (que coloca órdenes) SOLO se
        construye con TRADING_ENABLED=true — en shadow el engine recibe executor=None y es
        estructuralmente incapaz de ejecutar. Best-effort: si cae, se registra y la task
        termina — NO tira el bot.
        """
        if not self.settings.MOTOR_1_ARBITRAGE_ENABLED:
            return  # opt-in; default off → no-op (no toca nada en prod)
        # Dar tiempo a que el WS se suscriba y aterricen los primeros snapshots del book.
        await asyncio.sleep(30)
        if self._capture is None:
            logger.error("motor1_arb: capture service no disponible → no arranca")
            return
        manager = getattr(self._capture, "_v2_manager", None)
        if manager is None:
            logger.error(
                "motor1_arb: OrderbookManagerV2 no disponible en el capture service → no arranca"
            )
            return
        try:
            from src.strategies.motor_1_arbitrage.engine import Motor1Engine

            # Capa A: el executor SOLO se construye con TRADING_ENABLED=true; en shadow el
            # engine recibe executor=None (incapaz de ejecutar).
            if self.settings.TRADING_ENABLED:
                async with KalshiRestClient() as client:
                    executor = ArbitrageExecutor(client, RiskManager())
                    await Motor1Engine(manager, executor=executor).run(self._stop_event)
            else:
                await Motor1Engine(manager).run(self._stop_event)
        except Exception as e:
            msg = f"motor1_arb runner: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)

    async def _run_motor2_shadow(self) -> None:
        """
        Motor 2 (consenso sportsbooks) — gateado por MOTOR_2_SPORTSBOOK_ENABLED.

        Por defecto SHADOW (solo detecta/graba). APUESTA real solo cuando se cumplen
        las DOS condiciones, en capas independientes:
          1. odds REALES (LiveOddsSource) — nunca apuesta sobre el fixture fake.
          2. TRADING_ENABLED=true → se construye el Motor2Executor (Capa A) y se inyecta.
        Con cualquiera de las dos en falso, el motor sigue siendo shadow puro.

        El flip a odds reales es POR CONFIG: con ODDS_API_KEY seteada → LiveOddsSource
        (odds reales, sport_keys/regions de settings); vacía → FakeOddsSource (fixture).
        Best-effort: si cae, se registra y la task termina — NO tira el bot.
        """
        if not self.settings.MOTOR_2_SPORTSBOOK_ENABLED:
            return  # opt-in; default off → no-op (no toca nada en prod)
        await asyncio.sleep(30)  # dejar que el primer discovery puebla el universo 1X2
        if self._capture is None:
            return
        try:
            from src.strategies.motor_2_consensus.executor import Motor2Executor
            from src.strategies.motor_2_consensus.fixtures import world_cup_demo_fixture
            from src.strategies.motor_2_consensus.poller import Motor2ShadowPoller
            from src.strategies.motor_2_consensus.sources import (
                FakeOddsSource,
                LiveOddsSource,
                RestKalshiQuoteSource,
            )

            # Universo de Motor 2: series de MOTOR2_SERIES (incluye MLB 2-way), NO el de
            # Motor REST (MULTI_SERIES, solo winner-take-all ≥3). Sin esto, MLB no entra al
            # funnel de Motor 2 por más sport_key/alias que haya.
            motor2_series = frozenset(
                s.strip() for s in self.settings.MOTOR2_SERIES.split(",") if s.strip()
            )
            kalshi_source = RestKalshiQuoteSource(
                lambda: self._capture.motor2_event_universe(motor2_series)
            )
            # FLIP POR CONFIG: con la API paga (ODDS_API_KEY) usa odds REALES; si no, el
            # fixture fake (shadow, jamás apuesta). Sin editar código para encender.
            if self.settings.ODDS_API_KEY:
                sport_keys = [
                    s.strip() for s in self.settings.ODDS_API_SPORT_KEYS.split(",") if s.strip()
                ]
                odds_source = LiveOddsSource(sport_keys, regions=self.settings.ODDS_API_REGIONS)
                logger.info(
                    f"motor2 odds=LIVE sports={sport_keys} regions={self.settings.ODDS_API_REGIONS}"
                )
            else:
                odds_source = FakeOddsSource(world_cup_demo_fixture())
                logger.info("motor2 odds=FAKE (ODDS_API_KEY no seteada → shadow con fixture)")

            # Umbral de edge tuneable por config (pp → fracción), una sola fuente de verdad.
            min_edge = self.settings.MOTOR_2_MIN_EDGE_PCT / 100.0
            # Capa A: el executor SOLO se construye con TRADING_ENABLED=true. En shadow
            # queda None → el poller jamás intenta apostar (y el muro Capa C de
            # place_order es la defensa final aunque algo llegara a colarse).
            if self.settings.TRADING_ENABLED:
                async with KalshiRestClient() as client:
                    executor = Motor2Executor(
                        client,
                        RiskManager(),
                        min_entry_cents=self.settings.MOTOR_2_MIN_ENTRY_CENTS,
                        underdog_filter_enabled=self.settings.MOTOR_2_UNDERDOG_FILTER_ENABLED,
                    )
                    poller = Motor2ShadowPoller(
                        kalshi_source,
                        odds_source,
                        min_edge=min_edge,
                        one_per_event=self.settings.MOTOR_2_ONE_BET_PER_EVENT,
                        executor=executor,
                    )
                    await poller.run(self._stop_event)
            else:
                poller = Motor2ShadowPoller(
                    kalshi_source,
                    odds_source,
                    min_edge=min_edge,
                    one_per_event=self.settings.MOTOR_2_ONE_BET_PER_EVENT,
                )
                await poller.run(self._stop_event)
        except Exception as e:
            msg = f"motor2 runner: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)

    async def _run_motor2_exits(self) -> None:
        """
        Motor 2 — cierre de posiciones (take-profit/trailing). Gateado por
        MOTOR_2_SPORTSBOOK_ENABLED (corre junto al shadow poller, gestionando lo que ya hay
        abierto). CAPA A: el executor (que vende) solo se construye con TRADING_ENABLED=true Y
        MOTOR_2_EXECUTION_ENABLED=true — en shadow detecta y loguea [MOTOR 2 TP SHADOW] con net
        de fees pero NUNCA vende. Best-effort: si cae, se registra y la task termina.
        """
        if not self.settings.MOTOR_2_SPORTSBOOK_ENABLED:
            return  # opt-in; default off → no-op (no toca nada en prod)
        await asyncio.sleep(25)  # dejar que init_db / boot terminen
        try:
            from src.strategies.motor_2_consensus.exit_engine import Motor2ExitEngine

            await Motor2ExitEngine(
                trading_enabled=(
                    self.settings.TRADING_ENABLED and self.settings.MOTOR_2_EXECUTION_ENABLED
                ),
                take_profit_enabled=self.settings.MOTOR_2_TAKE_PROFIT_ENABLED,
                tp_threshold=self.settings.MOTOR_2_TAKE_PROFIT_CENTS,
                trailing_enabled=self.settings.MOTOR_2_TRAILING_ENABLED,
                trailing_drop=self.settings.MOTOR_2_TRAILING_DROP_CENTS,
            ).run(self._stop_event)
        except Exception as e:
            msg = f"motor2_exits runner: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)

    async def _run_daily_pnl(self) -> None:
        """
        Daily P&L digest — gateado por DAILY_PNL_REPORT_ENABLED. ADVISORY ONLY: una vez/día
        agrega el P&L realizado por motor, lo persiste en DailyPnL y manda el digest a
        Telegram. NUNCA tradea ni toca capital. Best-effort: si cae, se registra y termina.
        """
        await asyncio.sleep(20)  # dejar que init_db / boot terminen
        try:
            from src.analytics.daily_pnl import DailyPnLLoop

            await DailyPnLLoop().run(self._stop_event)
        except Exception as e:
            msg = f"daily_pnl runner: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)

    async def _run_balance_refresh(self) -> None:
        """
        C-01 — sincroniza el capital base del RiskManager con el balance REAL de Kalshi (cash
        disponible): al arrancar y cada BALANCE_REFRESH_SECONDS. Read-only (GET balance),
        corre FUERA del _check_lock (no bloquea el gatekeeper). Best-effort: si la API falla
        se mantiene el último valor (el RiskManager cae a ACTIVE_CAPITAL_USD si nunca hubo
        uno). NO tira el bot si revienta.
        """
        from src.risk.manager import RiskManager

        await asyncio.sleep(5)  # dejar que el boot termine; trae el balance temprano
        interval = self.settings.BALANCE_REFRESH_SECONDS
        while not self._stop_event.is_set():
            try:
                await RiskManager.refresh_capital_from_balance()
            except Exception as e:
                # refresh ya es best-effort por dentro; este guard es la red final.
                logger.warning(f"balance_refresh tick: {type(e).__name__}: {e}")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return  # stop solicitado
            except TimeoutError:
                pass

    async def _run_analyst_loop(self) -> None:
        """
        Analyst Loop (loop engineering) — gateado por ANALYST_LOOP_ENABLED. ADVISORY ONLY:
        observa EdgeWindow + el embudo de Motor 2, computa un veredicto, lo persiste y manda
        un digest a Telegram. NUNCA tradea, cambia config ni mergea. Best-effort: si cae,
        se registra y la task termina — NO tira el bot.
        """
        await asyncio.sleep(15)  # dejar que init_db / boot terminen
        try:
            from src.analytics.analyst_loop import AnalystLoop

            await AnalystLoop().run(self._stop_event)
        except Exception as e:
            msg = f"analyst_loop runner: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)

    async def _run_memory_monitor(self) -> None:
        """
        Monitor de memoria — gateado por MEMORY_MONITOR_ENABLED. ADVISORY ONLY: lee el uso
        real del cgroup y avisa por Telegram cuando cruza MEMORY_MONITOR_THRESHOLD_PCT del
        límite del contenedor (aviso temprano de crecimiento del baseline antes del OOM).
        NUNCA tradea ni reinicia nada. Best-effort: si cae, se registra y la task termina.
        """
        if not self.settings.MEMORY_MONITOR_ENABLED:
            return  # opt-out; default on → no-op si se desactiva
        await asyncio.sleep(10)  # dejar que el boot termine
        try:
            from src.monitoring.memory_monitor import MemoryMonitor

            await MemoryMonitor().run(self._stop_event)
        except Exception as e:
            msg = f"memory_monitor runner: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)

    async def _reconcile_on_boot(self) -> None:
        """
        Reconcilia trades huérfanos post-crash.

        Tolerante a fallas: si Kalshi está caído al arranque, el bot SIGUE
        corriendo — los trades pending quedan sin resolver en DB, pero el
        operador ve el error en BotState.last_error y /status.
        """
        try:
            async with KalshiRestClient() as rest_client:
                risk_manager = RiskManager()
                executor = ArbitrageExecutor(
                    rest_client=rest_client,
                    risk_manager=risk_manager,
                )
                await executor.initialize()
                logger.success("Reconcile post-crash completado")
        except Exception as e:
            msg = f"Reconcile en boot falló: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)
            # NO re-raise: el bot debe seguir arrancando

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

            # Rehidratar el kill-switch PERSISTENTE: si quedó engaged (un kill-switch
            # previo + restart de Coolify), arrancar PAUSADO. La pausa en memoria no
            # sobrevive restarts; esta sí. check_pre_trade corta vía BotState.is_paused y
            # la Capa A no construye el executor. Despausa SOLO manual (clear_kill_switch.py).
            await self._rehydrate_kill_switch()

            # Fase 6 Motor 1: Reconciliación de trades huérfanos post-crash.
            await self._reconcile_on_boot()

            self._record_run_start()
            await alert_startup()

            # Lanzar servicios concurrentemente
            tasks = [
                asyncio.create_task(self._run_health_server(), name="health"),
                asyncio.create_task(self._run_data_capture(), name="capture"),
                asyncio.create_task(self._run_settlement(), name="settlement"),
                asyncio.create_task(self._run_motor2_shadow(), name="motor2_shadow"),
                asyncio.create_task(self._run_motor2_exits(), name="motor2_exits"),
                asyncio.create_task(self._run_balance_refresh(), name="balance_refresh"),
                asyncio.create_task(self._run_memory_monitor(), name="memory_monitor"),
            ]
            if self.settings.ANALYST_LOOP_ENABLED:
                tasks.append(asyncio.create_task(self._run_analyst_loop(), name="analyst_loop"))
            if self.settings.DAILY_PNL_REPORT_ENABLED:
                tasks.append(asyncio.create_task(self._run_daily_pnl(), name="daily_pnl"))
            if self.settings.MOTOR_3_CLV_ENABLED:
                tasks.append(asyncio.create_task(self._run_motor3_clv(), name="motor3_clv"))
            if self.settings.MOTOR_1_ARBITRAGE_ENABLED:
                tasks.append(asyncio.create_task(self._run_motor1_arb(), name="motor1_arb"))

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
