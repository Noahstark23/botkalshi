"""
Servicio de captura de datos.

Responsabilidad única: alimentar la DB con precios reales de Kalshi.
NO toma decisiones de trading. NO ejecuta órdenes.

Estrategia:
    1. Descubrir markets de interés (deportes, política, etc.)
    2. Suscribirse a orderbook_delta + ticker via WebSocket
    3. Snapshot completo cada 5 min via REST como fallback
    4. Persistir todo en SQLite

Esto corre 24/7. La data acumulada es input para los motores de trading.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from loguru import logger

from src.clients.kalshi_rest import KalshiRestClient
from src.clients.kalshi_ws import KalshiWebSocket
from src.monitoring.health import BotState
from src.storage.models import MarketSnapshot, OrderbookEvent, get_session
from src.utils.config import get_settings

# Series de Kalshi a trackear. Foco en mercados con liquidez decente.
# Ajustar según evolucione el roadmap.
TARGET_SERIES_PREFIXES = [
    # Deportes
    "KXMLB",   # MLB
    "KXNBA",   # NBA
    "KXNHL",   # NHL
    "KXNFL",   # NFL (en temporada)
    "KXEPL",   # Premier League
    "KXUCL",   # Champions League
    "KXUEL",   # Europa League
    # Política y eventos (donde menos competencia algorítmica)
    "KXPRES",
    "KXPOTUS",
]


class DataCaptureService:
    """Captura de datos en tiempo real, sin trading."""

    SNAPSHOT_INTERVAL_SEC = 300  # 5 min
    MAX_TICKERS_PER_SNAPSHOT_CYCLE = 50

    def __init__(self) -> None:
        self.settings = get_settings()
        self.ws = KalshiWebSocket()
        self._stop_event = asyncio.Event()
        self._tracked_tickers: set[str] = set()

    # =====================================================
    # WS event handlers
    # =====================================================

    async def _on_orderbook_delta(self, msg: dict[str, Any]) -> None:
        BotState.heartbeat()
        try:
            data = msg.get("msg", {})
            ticker = data.get("market_ticker")
            side = data.get("side")
            price = data.get("price")
            delta = data.get("delta")

            if not all([ticker, side, price is not None, delta is not None]):
                return

            with get_session() as s:
                event = OrderbookEvent(
                    ticker=ticker,
                    side=side,
                    price_cents=int(price),
                    delta=int(delta),
                )
                s.add(event)
                s.commit()
        except Exception:
            logger.exception("Error procesando orderbook_delta")
            BotState.record_error("orderbook_delta processing error")

    async def _on_ticker(self, msg: dict[str, Any]) -> None:
        BotState.heartbeat()
        # Por ahora solo heartbeat, no persistimos tickers (mucho volumen).
        # Si necesitamos en futuro, agregar tabla TickerEvent.

    async def _on_trade(self, msg: dict[str, Any]) -> None:
        BotState.heartbeat()
        # Trades públicos del market - útiles para CLV strategy futura

    # =====================================================
    # Discovery
    # =====================================================

    async def _discover_markets(self) -> None:
        """Descubre markets activos en las series target."""
        async with KalshiRestClient() as client:
            for prefix in TARGET_SERIES_PREFIXES:
                try:
                    events_resp = await client.list_events(
                        series_ticker=prefix, limit=100
                    )
                    events = events_resp.get("events", [])

                    for event in events:
                        for market in event.get("markets", []):
                            ticker = market.get("ticker")
                            status = market.get("status")
                            if ticker and status == "open":
                                self._tracked_tickers.add(ticker)
                except Exception as e:
                    logger.debug(f"No events for {prefix}: {e}")

        BotState.tracked_markets_count = len(self._tracked_tickers)
        logger.success(f"Tracking {len(self._tracked_tickers)} markets")

    # =====================================================
    # Snapshots periódicos (REST fallback)
    # =====================================================

    async def _periodic_snapshots(self) -> None:
        """
        Cada 5 min toma snapshot completo de todos los markets trackeados.
        Esto es fallback en caso de que el WS pierda eventos.
        """
        while not self._stop_event.is_set():
            try:
                await self._take_snapshots()
            except Exception:
                logger.exception("Error en snapshot cycle")
                BotState.record_error("snapshot cycle error")

            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.SNAPSHOT_INTERVAL_SEC,
                )

    async def _take_snapshots(self) -> None:
        """Una pasada de snapshots."""
        if not self._tracked_tickers:
            return

        async with KalshiRestClient() as client:
            tickers_to_snap = list(self._tracked_tickers)[: self.MAX_TICKERS_PER_SNAPSHOT_CYCLE]
            captured = 0

            for ticker in tickers_to_snap:
                try:
                    resp = await client.get_market(ticker)
                    market = resp.get("market", {})

                    with get_session() as s:
                        snap = MarketSnapshot(
                            ticker=ticker,
                            event_ticker=market.get("event_ticker", ""),
                            yes_bid=market.get("yes_bid", 0) or 0,
                            yes_ask=market.get("yes_ask", 0) or 0,
                            no_bid=market.get("no_bid", 0) or 0,
                            no_ask=market.get("no_ask", 0) or 0,
                            last_price=market.get("last_price"),
                            volume=market.get("volume", 0) or 0,
                            open_interest=market.get("open_interest", 0) or 0,
                        )
                        s.add(snap)
                        s.commit()
                        captured += 1
                except Exception as e:
                    logger.debug(f"Snapshot failed for {ticker}: {e}")

        logger.info(f"Snapshots: {captured}/{len(tickers_to_snap)}")

    # =====================================================
    # Lifecycle
    # =====================================================

    async def run(self) -> None:
        """Loop principal del servicio."""
        await self._discover_markets()

        if not self._tracked_tickers:
            logger.error("No se descubrieron markets - verificar series prefixes y status de API")
            return

        # Registrar handlers
        self.ws.on("orderbook_delta", self._on_orderbook_delta)
        self.ws.on("ticker", self._on_ticker)
        self.ws.on("trade", self._on_trade)

        # Encolar suscripciones (se aplicarán al conectar el WS)
        ticker_list = list(self._tracked_tickers)
        for i in range(0, len(ticker_list), 100):  # Kalshi acepta batches grandes
            batch = ticker_list[i : i + 100]
            self.ws.queue_subscription(
                channels=["orderbook_delta", "ticker"],
                market_tickers=batch,
            )

        BotState.capture_running = True

        # Correr WS y snapshots concurrentemente
        await asyncio.gather(
            self.ws.run(),
            self._periodic_snapshots(),
            return_exceptions=True,
        )

        BotState.capture_running = False

    async def stop(self) -> None:
        self._stop_event.set()
        await self.ws.stop()
