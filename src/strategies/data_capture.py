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
import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from src.clients.kalshi_rest import KalshiRestClient
from src.clients.kalshi_ws import KalshiWebSocket
from src.monitoring.health import BotState
from src.storage.disk_guard import DiskGuard
from src.storage.models import MarketSnapshot, OrderbookEvent, get_session
from src.utils.config import get_settings

TARGET_SERIES_PREFIXES = [
    # ⚠️ Series EXACTAS (cada string es un series_ticker literal para list_events).
    # HOTFIX 2026-06-12: el discovery por paginación amplia de #41 falló en producción
    # (Kalshi tiene >40k markets abiertos dominados por crypto; el cap de 200 páginas
    # se agotó sin alcanzar los deportivos → Tracking 0). Se restaura el flujo
    # conocido-bueno (#30, 301 markets) hasta implementar el prefiltro server-side
    # (GET /series) — P2 v2, pendiente de verificar el endpoint.
    # Deportes
    # NOTA 2026-06-15 (histograma de discovery, PR #58): el prefijo CRUDO de cada liga
    # (KXMLB/KXNBA/KXNHL) devuelve el FUTURO DE CAMPEÓN, no los partidos: 1 evento con
    # 1 market por equipo (KXMLB=1ev×30mkt = 30 equipos MLB, KXNHL=1ev×32mkt = 32 NHL).
    # Los PARTIDOS diarios viven bajo el sufijo GAME (patrón confirmado por KXWCGAME).
    # KXMLBGAME debe aparecer como Nev×~2.0mkt (2 markets por-equipo) → Motor 2 lo maneja ✓.
    "KXMLB",  # MLB — futuro de campeón (30-way multi-outcome)
    "KXMLBGAME",  # MLB — PARTIDOS (candidato a verificar en el próximo histograma)
    "KXNBA",  # NBA
    "KXNHL",  # NHL
    "KXNFL",  # NFL (en temporada)
    "KXEPL",  # Premier League
    "KXUCL",  # Champions League
    "KXUEL",  # Europa League
    # Mundial 2026 — series confirmadas contra la API pública (2026-06-11/13)
    "KXMENWORLDCUP",  # ganador del torneo (selecciones masculinas)
    "KXMWORLDCUP",
    "KXWCGAME",  # PARTIDOS 1X2 (Gana/Empata/Gana) — verificado 2026-06-13:
    # 69 eventos, exactamente 3 markets por evento (ej. -JOR/-ARG/-TIE).
    # EL target de alta frecuencia del multi-outcome shadow.
    "KXFIFAADVANCE",  # clasificación a siguiente ronda
    "KXFIFATOTAL",  # totales de goles
    "KXFIFASPREAD",  # hándicaps
    "KXWCGROUPWIN",  # ganador de grupo
    "KXWCSTAGE",  # alcance de ronda por equipo
    "KXWCTEAMGOALS",  # goles por equipo
    "KXWCGOALIEPEN",  # props (arqueros/penales)
    # Política y eventos (donde menos competencia algorítmica)
    "KXPRES",
    "KXPOTUS",
]


def parse_price_to_cents(value: object) -> int | None:
    """
    Convierte precio a centavos enteros.
    Acepta int directo (viejo: 27->27), str dollar fixed-point ("0.2700"->27),
    o float (0.27->27). Retorna None para None o input invalido.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(round(float(value) * 100))
        except (ValueError, TypeError):
            return None
    if isinstance(value, float):
        return int(round(value * 100))
    return None


def parse_size(value: object) -> int | None:
    """Tamano de un delta o nivel. Acepta str, int, float. Retorna None si invalido."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(round(float(value)))
        except (ValueError, TypeError):
            return None
    return None


def _top_bid(levels: list[Any]) -> tuple[int | None, int | None]:
    """
    Extrae el mejor bid (precio mas alto con size > 0) de una lista de levels.

    Levels formato: [["0.2700", "100.00"], ...] o [[price_cents, size], ...]

    Returns:
        (price_cents, size) o (None, None) si lista vacia o malformada.
    """
    best_price_cents: int | None = None
    best_size: int | None = None

    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price_cents = parse_price_to_cents(level[0])
        size = parse_size(level[1])
        if price_cents is None or size is None or size <= 0:
            continue
        if best_price_cents is None or price_cents > best_price_cents:
            best_price_cents = price_cents
            best_size = size

    return best_price_cents, best_size


# Generaciones de shape del REST GET /markets/{t}/orderbook (incidente 2026-07-15: la API
# migró a 'orderbook_fp' + 'yes_dollars'/'no_dollars' con precios en dólares-string y dejó
# ciegos a TODOS los consumidores que buscaban 'orderbook' + 'yes'/'no' — M5 sin cotizar,
# y peor: los brazos de salida de M2/M3 en fail-closed sin poder vender). Los VALORES los
# convierte parse_price_to_cents (int cents, float, o dólares-string); acá solo se
# normalizan las CLAVES, en orden nuevo→viejo.
_BOOK_WRAPPER_KEYS = ("orderbook_fp", "orderbook")
_BOOK_SIDE_KEYS: dict[str, tuple[str, ...]] = {
    "yes": ("yes_dollars", "yes_dollars_fp", "yes"),
    "no": ("no_dollars", "no_dollars_fp", "no"),
}


def rest_orderbook_sides(ob: object) -> dict[str, list]:
    """Normaliza el response del REST /orderbook a {'yes': levels, 'no': levels}.

    Tolera cualquier generación de shape (wrapper 'orderbook_fp' o 'orderbook', o el book
    ya desenvuelto) y cualquier variante de claves de lado. Los niveles quedan tal cual
    (los convierte _top_bid/parse_price_to_cents). Fail-safe: input raro → lados vacíos.
    """
    outer = ob if isinstance(ob, dict) else {}
    book = outer
    for wrapper in _BOOK_WRAPPER_KEYS:
        inner = outer.get(wrapper)
        if isinstance(inner, dict):
            book = inner
            break
    sides: dict[str, list] = {}
    for side, keys in _BOOK_SIDE_KEYS.items():
        levels: list = []
        for key in keys:
            value = book.get(key)
            if isinstance(value, list) and value:
                levels = value
                break
        sides[side] = levels
    return sides


class DataCaptureService:
    """Captura de datos en tiempo real, sin trading."""

    SNAPSHOT_INTERVAL_SEC = 300  # 5 min
    MAX_TICKERS_PER_SNAPSHOT_CYCLE = 50
    WS_SILENCE_THRESHOLD_SEC = 300  # silencio del WS que dispara reconexion forzada
    WS_ZOMBIE_ALERT_THRESHOLD = 2  # detecciones consecutivas antes de alertar a Telegram
    # P2 — re-discovery periódico (el método interno volvió a series exactas; hotfix)
    REDISCOVERY_INTERVAL_SEC = 6 * 3600  # re-discovery cada 6h (markets nuevos del torneo)

    def __init__(self, rest_client: KalshiRestClient | None = None) -> None:
        self.settings = get_settings()
        self.ws = KalshiWebSocket()
        self._stop_event = asyncio.Event()
        self._tracked_tickers: set[str] = set()
        self._delta_shape_logged = False
        self._snapshot_shape_logged = False
        self._v2_manager = None  # OrderbookManagerV2 | None, set if USE_ORDERBOOK_MANAGER_V2
        # close_time (ISO) por ticker desde discovery → se alimenta al v2_manager para que la
        # recovery NO pida snapshot de mercados settled (causa del loop de code 15).
        self._market_close_times: dict[str, str] = {}
        self._market_keys_logged = False  # DIAG: loguear una vez qué campos trae get_event
        self._rest_engine = None  # RestArbEngine | None, set if MOTOR_REST_ENABLED (shadow)
        # Cliente REST persistente inyectado por el runner SOLO con TRADING_ENABLED=true.
        # None en shadow. La Capa 2 lo pasará al RestExecutor del Motor REST.
        self._rest_client = rest_client
        self._ws_zombie_count = 0  # detecciones consecutivas de WS zombie (reset al recuperar)
        # Motor 8 (F1 shadow, opcional): OFI en memoria, pasajero del feed de deltas.
        # Requiere el manager V2 para medir mids (sin él, el experimento no tiene resultado).
        self._ofi = None
        if self.settings.MOTOR_8_OFI_ENABLED:
            from src.strategies.motor_8_ofi.shadow import Motor8OfiShadow

            self._ofi = Motor8OfiShadow(
                self._mid_of,
                window_sec=self.settings.MOTOR_8_OFI_WINDOW_SEC,
                z_min=self.settings.MOTOR_8_OFI_Z_MIN,
                min_baseline=self.settings.MOTOR_8_OFI_MIN_BASELINE,
                cooldown_sec=self.settings.MOTOR_8_OFI_COOLDOWN_SEC,
            )
            logger.info("motor8.ofi shadow armado (F1: solo observa y mide)")

    def _mid_of(self, ticker: str) -> float | None:
        """MID del ticker en cents desde el manager V2 (memoria). None si el book no está
        sano (uninit/stale/cuarentena) — el experimento OFI descarta antes que medir basura."""
        v2 = self._v2_manager
        if v2 is None:
            return None
        yes = v2.get_top_of_book(ticker, "yes")
        no = v2.get_top_of_book(ticker, "no")
        if yes is None or no is None or yes.best_bid is None or no.best_bid is None:
            return None
        yes_bid = yes.best_bid.price_cents
        yes_ask = 100 - no.best_bid.price_cents
        if not (1 <= yes_bid <= 99 and 1 <= yes_ask <= 99):
            return None
        return (yes_bid + yes_ask) / 2.0

    def multi_event_universe(self) -> dict[str, set[str]]:
        """
        Seam read-only para el Motor REST: {event_key → tickers hermanos} de los partidos
        multi-outcome (1X2/winner) ya descubiertos. Filtra por MULTI_SERIES (winner-take-all
        ≥3 legs). No toca red ni estado.
        """
        Seam read-only GENÉRICO: {event_key → tickers hermanos} de los tickers trackeados
        cuya SERIE (primer segmento del ticker, igualdad exacta) está en `series`. Agrupa
        por event_key (ticker sin el sufijo de outcome). No toca red ni estado.

        return self._event_universe_for(RestArbEngine.multi_series_from_settings())

    def motor2_event_universe(self, series: frozenset[str]) -> dict[str, set[str]]:
        """
        Seam read-only para el MOTOR 2: {event_key → tickers hermanos} de las series que
        Motor 2 consume. DESACOPLADO de MULTI_SERIES — incluye 2-way (MLB moneyline,
        KXMLBGAME) además de los multi-outcome. `series` viene de MOTOR2_SERIES (config).
        """
        return self._event_universe_for(series)

    def _event_universe_for(self, series: frozenset[str]) -> dict[str, set[str]]:
        """Agrupa los tickers trackeados de `series` por event_key (rsplit). Helper común."""
        universe: dict[str, set[str]] = {}
        for t in self._tracked_tickers:
            if t.split("-", 1)[0] not in series:
                continue
            universe.setdefault(t.rsplit("-", 1)[0], set()).add(t)
        return universe

    def series_histogram(self) -> dict[str, tuple[int, float]]:
        """
        Foto read-only por serie: {prefijo → (n_eventos, markets/evento)}.

        Computada de self._tracked_tickers agrupando ticker→evento (rsplit, igual que
        multi_event_universe) y ticker→serie (split). Revela DOS incógnitas de prod que el
        repo no puede saber sin red — el gate para onboardear MLB al Motor 2:
          (a) el PREFIJO exacto de cada familia (ej. KXMLBGAME vs KXMLB...), y
          (b) la ESTRUCTURA: ~2.0 mkts/evento = 2 markets por-equipo (lo que
              _parse_event_quotes ya maneja ✓); ~1.0 = 1 market 2-way (necesitaría variante).
        Sport-agnóstica y sin red: leer este histograma ES el diagnóstico de MLB.
        """
        events_by_prefix: dict[str, set[str]] = {}
        markets_by_prefix: dict[str, int] = {}
        for t in self._tracked_tickers:
            prefix = t.split("-", 1)[0]
            events_by_prefix.setdefault(prefix, set()).add(t.rsplit("-", 1)[0])
            markets_by_prefix[prefix] = markets_by_prefix.get(prefix, 0) + 1
        return {
            prefix: (len(events), markets_by_prefix[prefix] / len(events))
            for prefix, events in events_by_prefix.items()
        }

    # =====================================================
    # WS event handlers
    # =====================================================

    async def _on_orderbook_delta(self, msg: dict[str, Any]) -> None:
        BotState.heartbeat()
        try:
            data = msg.get("msg", {})

            if not self._delta_shape_logged:
                logger.info(f"orderbook_delta shape detected: keys={list(data.keys())}")
                self._delta_shape_logged = True

            ticker = data.get("market_ticker")
            side = data.get("side")

            # Shape 2026 (Kalshi actual): "price_dollars" + "delta_fp" como strings en dolares
            # Shape legacy (defensivo): "price" + "delta" como ints en cents
            price_raw = data.get("price_dollars", data.get("price"))
            delta_raw = data.get("delta_fp", data.get("delta"))
            # WS payload usa fixed-point strings (price_dollars="0.4200", delta_fp="-2500.00").
            # DB schema usa integers en cents (price_cents=42, delta=-2500). La conversión
            # vive acá. Si futuro refactor toca persist layer, mantener esta semántica
            # o migrar schema explícitamente. Documentado 2026-05-19.
            price_cents = parse_price_to_cents(price_raw)
            delta_size = parse_size(delta_raw)

            if not all([ticker, side, price_cents is not None, delta_size is not None]):
                sample_keys = list(data.keys())
                logger.warning(
                    f"orderbook_delta missing required fields. Keys present: {sample_keys}. "
                    f"Sample: {str(data)[:300]}"
                )
                BotState.record_error(f"orderbook_delta unknown shape: keys={sample_keys}")
                return

            # Motor 8 (F1 shadow, opcional): OFI pasajero del feed — internamente best-effort,
            # jamás rompe la captura.
            if self._ofi is not None:
                self._ofi.observe_delta(ticker, side, delta_size)

            # Persistir orderbook_events es OPT-IN (default off, incidente disco-lleno 2026-07-10):
            # una fila por cada delta del WS (~240 tickers, 24/7) = millones/día, y NADIE la lee
            # (telemetry write-only). Los books viven en memoria (OrderbookManagerV2). Solo se graba
            # si PERSIST_ORDERBOOK_EVENTS=true (debug puntual, con retención via _run_db_maintenance).
            # DiskGuard: en disco CRITICAL la telemetría se descarta (backpressure) — trading no.
            if self.settings.PERSIST_ORDERBOOK_EVENTS and DiskGuard.diagnostics_allowed():
                with get_session() as s:
                    event = OrderbookEvent(
                        ticker=ticker,
                        side=side,
                        price_cents=price_cents,
                        delta=delta_size,
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

    async def _on_orderbook_snapshot(self, msg: dict[str, Any]) -> None:
        """
        Snapshot inicial del orderbook. Extrae top-of-book y persiste a MarketSnapshot.

        Kalshi envia un snapshot al suscribirse antes de empezar a mandar deltas.
        Usar campos yes_dollars_fp / no_dollars_fp (shape 2026) con fallback
        a yes / no (shape viejo de cents).
        """
        BotState.heartbeat()

        if not self._snapshot_shape_logged:
            data_check = msg.get("msg", {})
            logger.info(f"orderbook_snapshot shape detected: keys={list(data_check.keys())}")
            self._snapshot_shape_logged = True

        try:
            data = msg.get("msg", {})
            ticker = data.get("market_ticker")
            if not ticker:
                return

            # Shape 2026: yes_dollars_fp / no_dollars_fp
            # Shape viejo: yes / no (lista de [price_cents, size])
            yes_levels = data.get("yes_dollars_fp") or data.get("yes") or []
            no_levels = data.get("no_dollars_fp") or data.get("no") or []

            yes_bid_cents, _yes_size = _top_bid(yes_levels)
            no_bid_cents, _no_size = _top_bid(no_levels)

            # En Kalshi modelo reciproco: ask de yes = 100 - bid de no
            yes_ask_cents = (100 - no_bid_cents) if no_bid_cents is not None else None
            no_ask_cents = (100 - yes_bid_cents) if yes_bid_cents is not None else None

            # DiskGuard: market_snapshots es DIAGNÓSTICO — en disco critical se descarta.
            if not DiskGuard.diagnostics_allowed():
                return

            with get_session() as s:
                snap = MarketSnapshot(
                    ticker=ticker,
                    event_ticker="",  # WS snapshot no incluye event_ticker
                    yes_bid=yes_bid_cents or 0,
                    yes_ask=yes_ask_cents or 0,
                    no_bid=no_bid_cents or 0,
                    no_ask=no_ask_cents or 0,
                    last_price=None,
                    volume=0,
                    open_interest=0,
                )
                s.add(snap)
                s.commit()
        except Exception:
            logger.exception("Error procesando orderbook_snapshot")
            BotState.record_error("orderbook_snapshot processing error")

    # =====================================================
    # Discovery
    # =====================================================

    async def _discover_markets(self) -> set[str]:
        """
        Descubre markets activos por SERIES EXACTAS y devuelve los NUEVOS (delta).

        HOTFIX: flujo conocido-bueno restaurado de #30 (list_events(series_ticker=X)
        + get_event por evento, pausas anti-429). Lento (~minutos) pero CORRECTO —
        el listado amplio de #41 nunca alcanzaba los markets deportivos (>40k markets
        abiertos dominados por crypto). Conserva la firma delta de #41: el
        re-discovery periódico la usa para suscribir solo lo nuevo en caliente.
        """
        before = set(self._tracked_tickers)
        per_series: dict[str, int] = {}
        errors_by_prefix: dict[str, str] = {}
        async with KalshiRestClient() as client:
            for prefix in TARGET_SERIES_PREFIXES:
                try:
                    # limit=200 (máx de Kalshi): el Mundial tiene 104 partidos — una serie
                    # como KXFIFAGAME clipea con limit=100 y perderíamos eventos.
                    events_resp = await client.list_events(series_ticker=prefix, limit=200)
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
                                # DIAG (una vez): qué campos trae el market de get_event — confirma
                                # si 'close_time' está presente (si no, el filtro de purga nunca
                                # tiene con qué marcar settled → purged=0).
                                if not self._market_keys_logged and ticker:
                                    self._market_keys_logged = True
                                    logger.info(
                                        f"v2.discovery market keys={sorted(market.keys())} "
                                        f"close_time={market.get('close_time')!r}"
                                    )
                                if ticker and status in ("open", "active"):
                                    self._tracked_tickers.add(ticker)
                                    self._market_close_times[ticker] = market.get("close_time")
                                    per_series[prefix] = per_series.get(prefix, 0) + 1
                        except Exception as e:
                            logger.warning(
                                f"get_event({event_ticker}) error: {type(e).__name__}: {e}"
                            )
                except Exception as e:
                    errors_by_prefix[prefix] = type(e).__name__
                    logger.warning(f"Discovery error en {prefix}: {type(e).__name__}: {e}")
                await asyncio.sleep(2.0)  # pausa entre prefixes

        if errors_by_prefix:
            logger.warning(f"Discovery con {len(errors_by_prefix)} errores: {errors_by_prefix}")
        # Alimentar close_time al v2_manager (si ya existe) para purgar settled de la recovery.
        if self._v2_manager is not None:
            self._v2_manager.set_close_times(self._market_close_times)
        new_tickers = self._tracked_tickers - before
        BotState.tracked_markets_count = len(self._tracked_tickers)
        logger.success(
            f"Tracking {len(self._tracked_tickers)} markets (+{len(new_tickers)} nuevos)"
        )
        # Conteo por serie: evidencia de qué familias están vivas (ej. KXFIFAGAME).
        logger.info(f"Discovery por serie: {dict(sorted(per_series.items()))}")
        # Estructura por serie (prefijo, n_eventos, mkts/evento): revela el prefijo exacto
        # de MLB y si los partidos son 2-market-por-equipo (~2.0 ✓) o 1-market-2-way (~1.0).
        hist = self.series_histogram()
        logger.info(
            "Discovery estructura: "
            + ", ".join(f"{p}={n}ev×{mpe:.1f}mkt" for p, (n, mpe) in sorted(hist.items()))
        )
        return new_tickers

    async def _run_rediscovery(self) -> None:
        """
        Re-discovery periódico (P2): toma markets listados DESPUÉS del boot — clave en
        un torneo de un mes (partidos nuevos cada día) con uptime continuo.

        BEST-EFFORT: un fallo de ciclo se loguea/registra y el loop SIGUE (no tira el
        servicio — el bot continúa capturando lo ya suscripto). Los tickers nuevos se
        suscriben EN CALIENTE: queue_subscription envía el subscribe inmediatamente si
        el WS está conectado y lo re-aplica en cada reconexión.
        """
        while not self._stop_event.is_set():
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.REDISCOVERY_INTERVAL_SEC
                )
            if self._stop_event.is_set():
                return
            try:
                new_tickers = await self._discover_markets()
                if new_tickers:
                    batch_list = sorted(new_tickers)
                    for i in range(0, len(batch_list), 100):
                        self.ws.queue_subscription(
                            channels=["orderbook_delta", "ticker"],
                            market_tickers=batch_list[i : i + 100],
                        )
                    logger.success(f"Re-discovery: {len(new_tickers)} markets nuevos suscriptos")
                    # P3: refrescar el universo multi-outcome con los markets nuevos.
                    if self._rest_engine is not None:
                        self._rest_engine.update_universe(self._tracked_tickers)
            except Exception as e:
                msg = f"Re-discovery fallo: {type(e).__name__}: {e}"
                logger.warning(msg)
                BotState.record_error(msg)

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
        # DiskGuard: market_snapshots es DIAGNÓSTICO — en disco critical se saltea la pasada
        # entera (ni siquiera pega a la API: el snapshot solo existe para persistirse).
        if not DiskGuard.diagnostics_allowed():
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
                            )
                            or 0,
                            yes_ask=parse_price_to_cents(
                                market.get("yes_ask_dollars") or market.get("yes_ask")
                            )
                            or 0,
                            no_bid=parse_price_to_cents(
                                market.get("no_bid_dollars") or market.get("no_bid")
                            )
                            or 0,
                            no_ask=parse_price_to_cents(
                                market.get("no_ask_dollars") or market.get("no_ask")
                            )
                            or 0,
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

        Ante un WS zombie fuerza la reconexion via ws.force_reconnect() (cierra el socket
        sin detener el loop, que reconecta con su backoff) y, si el zombie persiste
        WS_ZOMBIE_ALERT_THRESHOLD ciclos consecutivos, alerta a Telegram.
        """
        if not self.ws.is_connected:
            BotState.ws_connected = False
            uptime = (datetime.now(UTC) - BotState.started_at).total_seconds()
            if uptime > 120 and not BotState.current_error():
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
        if silence_seconds <= self.WS_SILENCE_THRESHOLD_SEC:
            self._ws_zombie_count = 0
            return

        # WS zombie: conectado pero sin trafico. Forzar reconexion.
        self._ws_zombie_count += 1
        logger.warning(
            f"ws.zombie.detected silence_seconds={int(silence_seconds)} "
            f"ws_is_connected={self.ws.is_connected} "
            f"zombie_count={self._ws_zombie_count} action_taken=force_reconnect"
        )
        BotState.record_error(
            f"WS zombie: connected but no messages for {silence_seconds:.0f}s "
            f"(detection #{self._ws_zombie_count}, forcing reconnect)"
        )

        try:
            await self.ws.force_reconnect()
        except Exception:
            logger.exception("ws.force_reconnect fallo")

        if self._ws_zombie_count >= self.WS_ZOMBIE_ALERT_THRESHOLD:
            try:
                from src.monitoring.telegram_alerts import alert_error

                await alert_error(
                    f"WS zombie persistente: sin mensajes por {silence_seconds:.0f}s "
                    f"tras {self._ws_zombie_count} detecciones consecutivas. "
                    f"Reconexion forzada; revisar si el feed se recupera."
                )
            except Exception:
                logger.exception("Telegram alert (ws zombie) fallo")

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

    def _ensure_v2_manager(self) -> None:
        """Crea el OrderbookManagerV2 y lo publica en BotState. IDEMPOTENTE. Se llama TEMPRANO (en
        run(), antes del discovery) para cerrar la condición de carrera del /status: el discovery
        puede tardar minutos (o devolver 0 y reintentar), y hasta que corría _register_ws_handlers
        el BotState.v2_manager quedaba None → /status reportaba instance=missing pese a estar
        habilitado. Con la creación temprana, la instancia existe desde el arranque; los handlers
        del WS se registran aparte en _register_ws_handlers."""
        if self._v2_manager is not None:
            return
        if not (self.settings.USE_ORDERBOOK_MANAGER_V2 or self.settings.MOTOR_1_ARBITRAGE_ENABLED):
            return
        from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2

        self._v2_manager = OrderbookManagerV2(
            self.ws,
            recovery_timeout_sec=self.settings.ORDERBOOK_V2_RECOVERY_TIMEOUT_SEC,
            max_recovery_buffer=self.settings.ORDERBOOK_V2_MAX_RECOVERY_BUFFER,
        )
        BotState.v2_manager = self._v2_manager
        logger.info("OrderbookManagerV2 creado + publicado en BotState (pre-discovery)")

    def _register_ws_handlers(self) -> None:
        """
        Registra los handlers del WS. Separado de run() para testear el wiring.

        - Handlers base de captura (siempre).
        - OrderbookManagerV2 si USE_ORDERBOOK_MANAGER_V2 y no Motor 1.
        - Motor REST (shadow) si MOTOR_REST_ENABLED.
        """
        # Handlers base de captura
        self.ws.on("orderbook_delta", self._on_orderbook_delta)
        self.ws.on("orderbook_snapshot", self._on_orderbook_snapshot)
        self.ws.on("ticker", self._on_ticker)
        self.ws.on("trade", self._on_trade)

        # Orderbook manager V2: la INSTANCIA ya se creó (idempotente) — típicamente en run() ANTES
        # del discovery, para que /status no reporte instance=missing durante el arranque. Acá solo
        # se registran sus 4 handlers en el WS (lifecycle del feed = del WS). Motor 1 lo LEE desde
        # self._v2_manager / BotState; NO lo reconstruye.
        self._ensure_v2_manager()
        if self._v2_manager is not None:
            self.ws.on("orderbook_delta", self._v2_manager.handle_message)
            self.ws.on("orderbook_snapshot", self._v2_manager.handle_message)
            self.ws.on("ok", self._v2_manager.handle_message)
            self.ws.on("error", self._v2_manager.handle_message)
            logger.info(
                f"OrderbookManagerV2 registered (v2_flag={self.settings.USE_ORDERBOOK_MANAGER_V2}, "
                f"motor1={self.settings.MOTOR_1_ARBITRAGE_ENABLED})"
            )

        # Motor REST — shadow mode. Estrictamente condicionado por MOTOR_REST_ENABLED.
        # SHADOW: detecta arbitraje sobre el canal `ticker` y graba EdgeWindow; NUNCA
        # ejecuta órdenes (TRADING_ENABLED es el muro; el engine no instancia ejecución).
        # El canal `ticker` ya se suscribe, así que solo se agrega el handler.
        if self.settings.MOTOR_REST_ENABLED:
            # ── Capa A del muro (gatear la construcción del path de ejecución) ──────
            # El executor + risk manager SOLO se construyen con TRADING_ENABLED=true Y
            # cliente REST persistente presente Y kill-switch persistente NO engaged. Si
            # falta cualquiera, el engine queda en SHADOW (executor=None) → incapaz de ejecutar.
            # Kill-switch: fail-safe-hacia-shadow — si la lectura falla, se asume engaged
            # (no construir). Cubre el restart de Coolify tras un kill-switch a las 3am.
            from src.storage.models import kill_switch_engaged
            from src.strategies.motor_rest_arb.engine import RestArbEngine

            try:
                ks_engaged, ks_reason = kill_switch_engaged()
            except Exception:
                ks_engaged, ks_reason = True, "lectura del kill-switch falló (fail-safe)"
                logger.exception("Motor REST: no se pudo leer el kill-switch persistente")

            executor = None
            risk_manager = None
            # CAPA A: ejecutar requiere el CONSENSO de dos flags — TRADING_ENABLED (global) Y
            # MOTOR_REST_EXECUTION_ENABLED (propio del motor). Con el propio en False el motor
            # corre en SHADOW aunque el trading global esté ON → valida el guardarraíl
            # pata-dura-primero (#85) sin ejecutar ni apagar a los otros motores.
            exec_requested = (
                self.settings.TRADING_ENABLED and self.settings.MOTOR_REST_EXECUTION_ENABLED
            )
            if exec_requested and self._rest_client is not None and not ks_engaged:
                from src.risk.manager import RiskManager
                from src.strategies.motor_rest_arb.executor import RestExecutor

                executor = RestExecutor(self._rest_client)
                risk_manager = RiskManager()
            elif self.settings.TRADING_ENABLED and not self.settings.MOTOR_REST_EXECUTION_ENABLED:
                logger.info(
                    "Motor REST: MOTOR_REST_EXECUTION_ENABLED=false → SHADOW "
                    "(detecta y graba EdgeWindow; valida el guardarraíl sin ejecutar órdenes)."
                )
            elif exec_requested and ks_engaged:
                logger.critical(
                    f"Motor REST: kill-switch persistente ENGAGED ({ks_reason}) → SHADOW "
                    "forzado. Resolver con scripts/clear_kill_switch.py + redeploy."
                )
            elif exec_requested and self._rest_client is None:
                logger.error(
                    "Motor REST: ejecución pedida (ambos flags ON) pero SIN cliente REST "
                    "persistente → se queda en SHADOW (no se construye el executor)."
                )

            self._rest_engine = RestArbEngine(executor=executor, risk_manager=risk_manager)
            # P3: universo de eventos multi-outcome (1X2/winner) — el discovery del boot
            # ya corrió cuando se registran los handlers; el re-discovery lo refresca.
            self._rest_engine.update_universe(self._tracked_tickers)
            self.ws.on("ticker", self._rest_engine.on_ticker)
            mode = "LIVE: ejecuta" if executor is not None else "SHADOW: detecta y graba EdgeWindow"
            logger.info(
                f"Motor REST registered ({mode}, trading_enabled={self.settings.TRADING_ENABLED})"
            )

    async def run(self) -> None:
        """Loop principal del servicio."""
        # Publicar el OrderbookManagerV2 en BotState ANTES del discovery: el discovery puede tardar
        # minutos (o devolver 0 y reintentar), y hasta que corría _register_ws_handlers el /status
        # reportaba instance=missing por la carrera. Idempotente con el registro posterior.
        self._ensure_v2_manager()
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

        self._register_ws_handlers()

        # El v2_manager recién se creó en _register_ws_handlers → empujarle los close_time ya
        # descubiertos al boot (la discovery previa corrió sin manager).
        if self._v2_manager is not None:
            self._v2_manager.set_close_times(self._market_close_times)

        # Encolar suscripciones (se aplicaran al conectar el WS)
        ticker_list = list(self._tracked_tickers)
        for i in range(0, len(ticker_list), 100):
            batch = ticker_list[i : i + 100]
            self.ws.queue_subscription(
                channels=["orderbook_delta", "ticker"],
                market_tickers=batch,
            )

        BotState.capture_running = True
        BotState.last_capture_running_true_at = time.monotonic()

        # Supervisor pattern: excepciones reportadas y re-levadas, nunca tragadas
        ws_task = asyncio.create_task(self._run_ws_supervised(), name="ws_supervisor")
        snap_task = asyncio.create_task(self._run_snapshots_supervised(), name="snap_supervisor")
        # Re-discovery (P2): best-effort con manejo interno de errores — solo termina
        # con el stop_event, igual que stop_task, así que no dispara el FIRST_COMPLETED.
        rediscovery_task = asyncio.create_task(self._run_rediscovery(), name="rediscovery")
        stop_task = asyncio.create_task(self._stop_event.wait(), name="stop_waiter")

        try:
            done, pending = await asyncio.wait(
                [ws_task, snap_task, rediscovery_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if not t.cancelled():
                    exc = t.exception()
                    if exc and not isinstance(exc, asyncio.CancelledError):
                        logger.error(f"Subloop {t.get_name()} crashed: {type(exc).__name__}: {exc}")
        finally:
            BotState.capture_running = False
            BotState.ws_connected = False

    async def stop(self) -> None:
        self._stop_event.set()
        await self.ws.stop()
