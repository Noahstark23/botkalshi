"""
Cliente WebSocket para Kalshi.

Recibe orderbook deltas, tickers, fills, etc. en tiempo real.

Features:
    - Reconexion automatica con backoff exponencial
    - Re-suscripcion tras reconexion
    - Handlers registrables por tipo de mensaje
    - Heartbeat tracking para detectar conexiones zombie
    - Shutdown limpio con stop()
    - Escalacion a Telegram tras N fallos consecutivos
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import websockets
import websockets.exceptions
from loguru import logger
from websockets.asyncio.client import ClientConnection
from websockets.connection import State as WsState

from src.auth.signer import KalshiSigner
from src.monitoring.health import BotState
from src.utils.config import get_settings

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

STABLE_CONNECTION_THRESHOLD_SEC = 60


class KalshiWebSocket:
    """
    Cliente WebSocket asincrono para Kalshi.

    Channels disponibles:
        orderbook_delta:   cambios incrementales en orderbooks
        ticker:            actualizaciones de mid price
        trade:             trades ejecutados (publicos)
        fill:              fills de tus propias ordenes
        market_lifecycle:  cambios de estado de markets
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.signer = KalshiSigner(
            private_key_path=self.settings.KALSHI_PRIVATE_KEY_PATH,
            api_key_id=self.settings.KALSHI_API_KEY_ID,
        )
        self.url = self.settings.ws_url

        self._ws: ClientConnection | None = None
        self._handlers: dict[str, list[EventHandler]] = {}
        self._running = False
        self._next_id = 1
        # Las suscripciones se guardan para re-aplicar tras reconexion
        self._subscriptions: list[dict[str, Any]] = []
        self._last_message_at: datetime | None = None

    # =====================================================
    # API publica
    # =====================================================

    def on(self, msg_type: str, handler: EventHandler) -> None:
        """
        Registra un handler para un tipo de mensaje.

        Args:
            msg_type: el field "type" del mensaje (ej: "orderbook_delta")
            handler: async function que recibe el mensaje completo
        """
        self._handlers.setdefault(msg_type, []).append(handler)

    def queue_subscription(
        self,
        channels: list[str],
        market_tickers: list[str] | None = None,
    ) -> None:
        """
        Encola una suscripcion que se aplicara al conectar.
        Si ya estamos conectados, tambien la envia inmediatamente.
        """
        params: dict[str, Any] = {"channels": channels}
        if market_tickers:
            params["market_tickers"] = market_tickers

        self._subscriptions.append(params)

        # Si ya estamos conectados, enviar ahora
        if self._ws is not None and self._ws.state == WsState.OPEN:
            asyncio.create_task(self._send_subscribe(params))

    @property
    def last_message_at(self) -> datetime | None:
        """Timestamp del ultimo mensaje recibido (para health checks)."""
        return self._last_message_at

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._ws.state == WsState.OPEN

    # =====================================================
    # Lifecycle
    # =====================================================

    async def run(self) -> None:
        """Loop principal con reconexion automatica y escalacion tras fallos consecutivos."""
        self._running = True
        backoff = 1.0
        max_backoff = 60.0
        consecutive_failures = 0
        alert_after_failures = 5
        alerted = False

        while self._running:
            connected_at: datetime | None = None
            try:
                connected_at = datetime.now(UTC)
                await self._connect_and_listen()
                consecutive_failures = 0
                alerted = False
                backoff = 1.0
            except asyncio.CancelledError:
                logger.info("WS run cancelled")
                raise
            except websockets.ConnectionClosed as e:
                stable_s = _stable_connection(connected_at)
                if stable_s:
                    logger.info(
                        f"ws.failure_counter.reset reason=stable_connection "
                        f"stable_seconds={int(stable_s)} previous_failures={consecutive_failures}"
                    )
                    consecutive_failures = 0
                    backoff = 1.0
                    alerted = False
                consecutive_failures += 1
                logger.warning(f"WS closed: {e}. Consecutive failures: {consecutive_failures}")
            except Exception:
                stable_s = _stable_connection(connected_at)
                if stable_s:
                    logger.info(
                        f"ws.failure_counter.reset reason=stable_connection "
                        f"stable_seconds={int(stable_s)} previous_failures={consecutive_failures}"
                    )
                    consecutive_failures = 0
                    backoff = 1.0
                    alerted = False
                consecutive_failures += 1
                logger.exception(
                    f"WS error inesperado. Consecutive failures: {consecutive_failures}"
                )

            if consecutive_failures >= alert_after_failures and not alerted:
                msg = (
                    f"WS failed {consecutive_failures} times consecutively. "
                    f"Manual intervention may be needed. URL: {self.url}"
                )
                logger.critical(msg)
                BotState.record_error(msg)
                try:
                    from src.monitoring.telegram_alerts import alert_error

                    await alert_error(msg)
                    alerted = True
                except Exception:
                    logger.exception("Telegram alert tambien fallo")

            if not self._running:
                break

            logger.info(f"Reconectando en {backoff:.1f}s (failures: {consecutive_failures})")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def stop(self) -> None:
        """Shutdown limpio. Idempotente."""
        self._running = False
        if self._ws and self._ws.state == WsState.OPEN:
            await self._ws.close()

    async def force_reconnect(self) -> None:
        """
        Cierra la conexion actual SIN detener el loop principal, forzando reconexion.

        A diferencia de stop() (que pone _running=False y hace que run() termine en
        el break de la linea ~175), esto solo cierra el socket: el `async for` de
        _connect_and_listen termina, run() lo trata como un cierre de conexion normal
        y reconecta con su backoff. Pensado para el watchdog cuando detecta un WS
        zombie (conectado pero sin trafico). Idempotente si no hay conexion abierta.
        """
        if self._ws is not None and self._ws.state == WsState.OPEN:
            logger.warning("ws.force_reconnect cerrando socket para forzar reconexion")
            await self._ws.close()

    # =====================================================
    # Internal
    # =====================================================

    async def _connect_and_listen(self) -> None:
        """Una sesion completa: conectar + suscribir + escuchar."""
        ws_path = "/trade-api/ws/v2"
        headers = self.signer.sign("GET", ws_path)

        logger.info(f"Conectando a {self.url}")
        BotState.ws_connected = False

        try:
            async with websockets.connect(
                self.url,
                additional_headers=headers,  # extra_headers fue renombrado en websockets >=13.0
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                max_size=2**22,  # 4MB
            ) as ws:
                self._ws = ws
                BotState.ws_connected = True
                self._last_message_at = datetime.now(UTC)
                logger.success(f"WS conectado a {self.url}")

                # Re-aplicar todas las suscripciones encoladas
                for params in self._subscriptions:
                    await self._send_subscribe(params)

                # Loop de escucha
                async for raw in ws:
                    self._last_message_at = datetime.now(UTC)
                    try:
                        msg = json.loads(raw)
                        await self._dispatch(msg)
                    except json.JSONDecodeError:
                        preview = (
                            raw[:200].decode("utf-8", errors="replace")
                            if isinstance(raw, bytes)
                            else raw[:200]
                        )
                        logger.error(f"JSON invalido: {preview}")
                    except Exception:
                        logger.exception("Error en dispatch")

        except websockets.exceptions.InvalidStatus as e:
            err_msg = (
                f"WS handshake rejected: HTTP {e.response.status_code}. "
                "Check signing path and credentials."
            )
            logger.error(err_msg)
            BotState.record_error(err_msg)
            raise
        except websockets.exceptions.InvalidHandshake as e:
            err_msg = f"WS handshake malformed: {e}"
            logger.error(err_msg)
            BotState.record_error(err_msg)
            raise
        except TypeError as e:
            # Regression catcher: websockets lib version mismatch (e.g. extra_headers in >=13)
            err_msg = f"WS TypeError (likely websockets lib version mismatch): {e}"
            logger.critical(err_msg)
            BotState.record_error(err_msg)
            raise
        finally:
            BotState.ws_connected = False
            self._ws = None

    async def send_command(
        self,
        cmd: str,
        *,
        action: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> int:
        """
        Send a JSON command and return the assigned request ID.

        Raises RuntimeError if not connected.
        """
        if self._ws is None or self._ws.state != WsState.OPEN:
            raise RuntimeError("WebSocket not connected")
        cmd_id = self._next_id
        self._next_id += 1
        msg: dict[str, Any] = {"id": cmd_id, "cmd": cmd}
        if action is not None:
            msg["action"] = action
        if params is not None:
            msg["params"] = params
        await self._ws.send(json.dumps(msg))
        return cmd_id

    async def _send_subscribe(self, params: dict[str, Any]) -> None:
        """Envia un comando subscribe."""
        if self._ws is None:
            return

        cmd_id = self._next_id
        self._next_id += 1

        msg = {"id": cmd_id, "cmd": "subscribe", "params": params}
        await self._ws.send(json.dumps(msg))

        n_tickers = len(params.get("market_tickers", []))
        logger.info(f"Subscribed: channels={params['channels']} markets={n_tickers}")

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Distribuye mensaje a handlers registrados."""
        msg_type = msg.get("type")

        if msg_type == "error":
            logger.error(f"Server error: {msg}")
            # Fall through — handlers (e.g. OrderbookManagerV2) may also process this

        if msg_type == "subscribed":
            logger.debug(f"Subscription confirmada: {msg}")
            return  # No handlers registered for subscribed

        if not msg_type:
            logger.debug(f"Mensaje sin type: {msg}")
            return

        handlers = self._handlers.get(msg_type, [])
        if not handlers:
            return

        # Ejecutar todos los handlers en paralelo, no fallar si uno revienta
        results = await asyncio.gather(
            *[h(msg) for h in handlers],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.opt(exception=r).error(f"Handler exception en {msg_type}: {r}")


def _stable_connection(connected_at: datetime | None) -> float:
    """
    Retorna segundos de conexión estable si superó el threshold, 0.0 si no.

    Un único datetime.now() para evitar doble computo de "now" en el caller.
    """
    if connected_at is None:
        return 0.0
    secs = (datetime.now(UTC) - connected_at).total_seconds()
    return secs if secs > STABLE_CONNECTION_THRESHOLD_SEC else 0.0
