"""
Servicio de captura de datos.

Responsabilidad unica: alimentar la DB con precios reales de Kalshi.
NO toma decisiones de trading. NO ejecuta ordenes.

Estrategia:
    1. Descubrir markets de interes (deportes, politica, etc.)
    2. Suscribirse a orderbook_delta + ticker via WebSocket
    3. Snapshot completo cada 5 min via REST como fallback
    4. Persistir todo en SQLite

Esto corre 24/7. La data acumulada es input para los motores de trading.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from src.clients.kalshi_rest import KalshiRestClient
from src.clients.kalshi_ws import KalshiWebSocket
from src.monitoring.health import BotState
from src.storage.models import MarketSnapshot, OrderbookEvent, get_session
from src.utils.config import get_settings

# Series de Kalshi a trackear. Reducido temporalmente a 2 para diagnostico de 429s.
# TODO: Restaurar lista completa una vez confirmado que discovery no genera burst.
TARGET_SERIES_PREFIXES = [
    "KXMLB",  # MLB
    "KXNBA",  # NBA
    # "KXNHL",   # NHL
    # "KXNFL",   # NFL (en temporada)
    # "KXEPL",   # Premier League
    # "KXUCL",   # Champions League
    # "KXUEL",   # Europa League
    # "KXPRES",  # Politica
    # "KXPOTUS", # Politica
]


def parse_price_to_cents(value: object) -> int:
    """Convierte precio de API (int, float, o string fixed-point) a centavos enteros."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(round(float(value) * 100))
        except ValueError:
            return 0
    return 0


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

    async def _on_trade(self, msg: dict[str, Any]) -> None:
        BotState.heartbeat()
        # Trades publicos del market - utiles para CLV strategy futura

    # =====================================================
    # Discovery
    # =====================================================

    async def _discover_markets(self) -> None:
        """Descubre markets activos en las series target.

        list_events() solo retorna metadatos del evento, NO markets[].
        Para obtener los markets hay que llamar get_event(event_ticker) por separado.
        2s de pausa entre requests para no generar burst de 429s.
        """
        errors_by_prefix: dict[str, str] = {}
        async with KalshiRestClient() as client:
            for prefix in TARGET_SERIES_PREFIXES:
                try:
                    events_resp = await client.list_events(series_ticker=prefix, limit=100)
                    events = events_resp.get("events", [])
                    for event in events:
                        event_ticker = event.get("event_ticker")
                        if not event_ticker:
                            continue
                        await asyncio.sleep(2.0)  # pausa entre get_event calls
                        try:
                            event_detail = await client.get_event(event_ticker)
                            # get_event retorna {"event": {...}, "markets": [...]}
                            # markets esta siempre en la raiz, NO dentro de "event"
                            markets = event_detail.get("markets", [])
                            for market in markets:
                                ticker = market.get("ticker")
                                status = market.get("status", "")
                                if ticker and status in ("open", "active"):
                                    self._tracked_tickers.add(ticker)
                            logger.debug(
                                f"Discovery {event_ticker}: {len(markets)} markets, "
                                f"{sum(1 for m in markets if m.get('status') in ('open','active'))} activos"
                            )
                        except Exception as e:
                            logger.warning(f"get_event({event_ticker}) error: {type(e).__name__}: {e}")
                except Exception as e:
                    errors_by_prefix[prefix] = type(e).__name__
                    logger.warning(f"Discovery error en {prefix}: {type(e).__name__}: {e}")
                await asyncio.sleep(2.0)  # pausa entre prefixes

        if errors_by_prefix:
            logger.warning(f"Discovery con {len(errors_by_prefix)} errores: {errors_by_prefix}")
        BotState.tracked_markets_count = len(self._tracked_tickers)
        logger.success(f"Tracking {len(self._tracked_tickers)} markets")

    # =====================================================
    # Snapshots periodicos (REST fallback)
    # =====================================================

    async def _periodic_snapshots(self) -> None:
        """
        Cada 5 min toma snapshot completo de todos los markets trackeados.
        Esto es fallback en caso de que el WS pierda eventos.
        """
        while not self._stop_event.is_set():
            try:
                await self._take_snapshots()
                await self._check_ws_health()
            except Exception as e:
                msg = f"Snapshot cycle error: {type(e).__name__}: {e}"
                logger.exception(msg)
                BotState.record_error(msg)

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
            failed = 0
            last_error_msg: str | None = None

            for ticker in tickers_to_snap:
                try:
                    resp = await client.get_market(ticker)
                    market = resp.get("market", {})

                    with get_session() as s:
                        snap = MarketSnapshot(
                            ticker=ticker,
                            event_ticker=market.get("event_ticker", ""),
                            # API V2 migro a fixed-point strings (e.g. "0.4500") en marzo 2026.
                            # Fallback a campos enteros legacy para compatibilidad.
                            yes_bid=parse_price_to_cents(
                                market.get("yes_bid_dollars") or market.get("yes_bid")
                            ),
                            yes_ask=parse_price_to_cents(
                                market.get("yes_ask_dollars") or market.get("yes_ask")
                            ),
                            no_bid=parse_price_to_cents(
                                market.get("no_bid_dollars") or market.get("no_bid")
                            ),
                            no_ask=parse_price_to_cents(
                                market.get("no_ask_dollars") or market.get("no_ask")
                            ),
                            last_price=market.get("last_price"),
                            volume=market.get("volume", 0) or 0,
                            open_interest=market.get("open_interest", 0) or 0,
                        )
                        s.add(snap)
                        s.commit()
                        captured += 1
                except Exception as e:
                    failed += 1
                    last_error_msg = f"{type(e).__name__}: {e}"
                    logger.debug(f"Snapshot failed for {ticker}: {e}")

        logger.info(f"Snapshots: {captured}/{len(tickers_to_snap)}")

        if captured == 0 and failed > 0:
            BotState.record_error(
                f"All {failed} snapshot attempts failed. Last error: {last_error_msg}"
            )

    async def _check_ws_health(self) -> None:
        """
        Detecta WS zombie (conectado pero sin trafico) o sin conexion tras periodo de gracia.
        """
        if not self.ws.is_connected:
            BotState.ws_connected = False
            uptime = (datetime.now(UTC) - BotState.started_at).total_seconds()
            if uptime > 120 and not BotState.last_error:
                BotState.record_error("WS not connected after 2min of uptime")
            return

        BotState.ws_connected = True

        last_msg = self.ws.last_message_at
        if last_msg is None:
            uptime = (datetime.now(UTC) - BotState.started_at).total_seconds()
            if uptime > 120:
                BotState.record_error(
                    "WS connected but zero messages received after 2min of uptime"
                )
            return

        silence_seconds = (datetime.now(UTC) - last_msg).total_seconds()
        if silence_seconds > 300:
            BotState.record_error(
                f"WS zombie: connected but no messages for {silence_seconds:.0f}s"
            )

    # =====================================================
    # Supervisors
    # =====================================================

    async def _run_ws_supervised(self) -> None:
        """Supervisa ws.run(). Re-leva excepciones despues de reportar."""
        try:
            await self.ws.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            msg = f"WS supervisor crashed: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)
            raise

    async def _run_snapshots_supervised(self) -> None:
        """Supervisa _periodic_snapshots(). Re-leva excepciones despues de reportar."""
        try:
            await self._periodic_snapshots()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            msg = f"Snapshots supervisor crashed: {type(e).__name__}: {e}"
            logger.exception(msg)
            BotState.record_error(msg)
            raise

    # =====================================================
    # Lifecycle
    # =====================================================

    async def run(self) -> None:
        """Loop principal del servicio."""
        # Discovery con retry: la primera llamada al arranque suele topar 429
        # si Kalshi tiene rate limit acumulado por deploys/restarts previos.
        backoff = 5.0
        max_backoff = 300.0  # 5 min cap
        while not self._stop_event.is_set():
            try:
                await self._discover_markets()
            except Exception as e:
                logger.warning(f"_discover_markets fallo: {type(e).__name__}: {e}")
            if self._tracked_tickers:
                break
            logger.warning(
                f"Discovery vacio - reintento en {backoff:.0f}s "
                f"(verifica series prefixes, rate limits, status API)"
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                return  # stop solicitado durante backoff
            except TimeoutError:
                pass
            backoff = min(backoff * 1.5, max_backoff)

        if not self._tracked_tickers:
            BotState.record_error("Discovery returned 0 markets after retries")
            return

        # Registrar handlers
        self.ws.on("orderbook_delta", self._on_orderbook_delta)
        self.ws.on("ticker", self._on_ticker)
        self.ws.on("trade", self._on_trade)

        # Encolar suscripciones (se aplicaran al conectar el WS)
        ticker_list = list(self._tracked_tickers)
        for i in range(0, len(ticker_list), 100):
            batch = ticker_list[i : i + 100]
            self.ws.queue_subscription(
                channels=["orderbook_delta", "ticker"],
                market_tickers=batch,
            )

        BotState.capture_running = True

        # Supervisor pattern: excepciones reportadas y re-levadas, nunca tragadas
        ws_task = asyncio.create_task(self._run_ws_supervised(), name="ws_supervisor")
        snap_task = asyncio.create_task(self._run_snapshots_supervised(), name="snap_supervisor")
        stop_task = asyncio.create_task(self._stop_event.wait(), name="stop_waiter")

        try:
            done, pending = await asyncio.wait(
                [ws_task, snap_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if not t.cancelled():
                    exc = t.exception()
                    if exc and not isinstance(exc, asyncio.CancelledError):
                        logger.error(
                            f"Subloop {t.get_name()} crashed: {type(exc).__name__}: {exc}"
                        )
        finally:
            BotState.capture_running = False
            BotState.ws_connected = False

    async def stop(self) -> None:
        self._stop_event.set()
        await self.ws.stop()
