"""
Cliente WebSocket para Kalshi.

Recibe orderbook deltas, tickers, fills, etc. en tiempo real.

Features:
    - Reconexión automática con backoff exponencial
    - Re-suscripción tras reconexión
    - Handlers registrables por tipo de mensaje
    - Heartbeat tracking para detectar conexiones zombie
    - Shutdown limpio con stop()
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import websockets
from loguru import logger
from websockets.client import WebSocketClientProtocol

from src.auth.signer import KalshiSigner
from src.utils.config import get_settings

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class KalshiWebSocket:
    """
    Cliente WebSocket asíncrono para Kalshi.

    Channels disponibles:
        orderbook_delta:   cambios incrementales en orderbooks
        ticker:            actualizaciones de mid price
        trade:             trades ejecutados (públicos)
        fill:              fills de tus propias órdenes
        market_lifecycle:  cambios de estado de markets
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.signer = KalshiSigner(
            private_key_path=self.settings.KALSHI_PRIVATE_KEY_PATH,
            api_key_id=self.settings.KALSHI_API_KEY_ID,
        )
        self.url = self.settings.ws_url

        self._ws: WebSocketClientProtocol | None = None
        self._handlers: dict[str, list[EventHandler]] = {}
        self._running = False
        self._next_id = 1
        # Las suscripciones se guardan para re-aplicar tras reconexión
        self._subscriptions: list[dict[str, Any]] = []
        self._last_message_at: datetime | None = None

    # =====================================================
    # API pública
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
        Encola una suscripción que se aplicará al conectar.
        Si ya estamos conectados, también la envía inmediatamente.
        """
        params: dict[str, Any] = {"channels": channels}
        if market_tickers:
            params["market_tickers"] = market_tickers

        self._subscriptions.append(params)

        # Si ya estamos conectados, enviar ahora
        if self._ws is not None and not self._ws.closed:
            asyncio.create_task(self._send_subscribe(params))

    @property
    def last_message_at(self) -> datetime | None:
        """Timestamp del último mensaje recibido (para health checks)."""
        return self._last_message_at

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # =====================================================
    # Lifecycle
    # =====================================================

    async def run(self) -> None:
        """Loop principal con reconexión automática."""
        self._running = True
        backoff = 1.0
        max_backoff = 60.0

        while self._running:
            try:
                await self._connect_and_listen()
                backoff = 1.0  # reset on clean disconnect
            except websockets.ConnectionClosed as e:
                logger.warning(f"WS closed (code={e.code}): {e.reason}")
            except asyncio.CancelledError:
                logger.info("WS run cancelled")
                raise
            except Exception:
                logger.exception("WS error inesperado")

            if not self._running:
                break

            logger.info(f"Reconectando en {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

    async def stop(self) -> None:
        """Shutdown limpio. Idempotente."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()

    # =====================================================
    # Internal
    # =====================================================

    async def _connect_and_listen(self) -> None:
        """Una sesión completa: conectar + suscribir + escuchar."""
        # WS auth - mismo path que REST con method GET
        ws_path = "/trade-api/ws/v2"
        headers = self.signer.sign("GET", ws_path)

        logger.info(f"Conectando a {self.url}")
        async with websockets.connect(
            self.url,
            extra_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
            max_size=2**22,  # 4MB
        ) as ws:
            self._ws = ws
            self._last_message_at = datetime.now(UTC)
            logger.success("WS conectado")

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
                    logger.error(f"JSON inválido: {raw[:200]}")
                except Exception:
                    logger.exception("Error en dispatch")

        self._ws = None

    async def _send_subscribe(self, params: dict[str, Any]) -> None:
        """Envía un comando subscribe."""
        if self._ws is None:
            return

        cmd_id = self._next_id
        self._next_id += 1

        msg = {"id": cmd_id, "cmd": "subscribe", "params": params}
        await self._ws.send(json.dumps(msg))

        n_tickers = len(params.get("market_tickers", []))
        logger.info(
            f"Subscribed: channels={params['channels']} markets={n_tickers}"
        )

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        """Distribuye mensaje a handlers registrados."""
        msg_type = msg.get("type")

        if msg_type == "error":
            logger.error(f"Server error: {msg}")
            return

        if msg_type == "subscribed":
            logger.debug(f"Subscription confirmada: {msg}")
            return

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
                logger.exception(f"Handler exception en {msg_type}: {r}")
