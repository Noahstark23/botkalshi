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
from src.strategies.data_capture import _top_bid
from src.strategies.motor_3_clv.detector import detect_and_log, summarize_exits
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor
from src.strategies.motor_3_clv.poller import PortfolioPoller
from src.strategies.motor_3_clv.take_profit import DEFAULT_TAKE_PROFIT_CENTS, take_profit_due


class Motor3Engine:
    """Bucle CLV: sincroniza cartera → detecta salidas a T-30min → (liquida si trading on)."""

    LOOP_INTERVAL_SEC = 60.0

    def __init__(
        self,
        *,
        trading_enabled: bool = False,
        take_profit_enabled: bool = False,
        tp_threshold: int = DEFAULT_TAKE_PROFIT_CENTS,
    ) -> None:
        self._poller = PortfolioPoller()
        self._trading_enabled = trading_enabled
        self._take_profit_enabled = take_profit_enabled
        self._tp_threshold = tp_threshold
        self._executor: Motor3ExitExecutor | None = None
        self._client: KalshiRestClient | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            f"motor3.engine started (trading_enabled={self._trading_enabled} "
            f"take_profit_enabled={self._take_profit_enabled} tp_threshold={self._tp_threshold}c)"
        )
        # El cliente REST se abre SIEMPRE: el take-profit necesita leer el orderbook
        # (bid del lado abierto) aun en shadow. La venta sigue gateada por la EXISTENCIA del
        # executor (CAPA A): solo se construye con trading on → en shadow lee pero jamás vende.
        async with KalshiRestClient() as client:
            self._client = client
            if self._trading_enabled:
                self._executor = Motor3ExitExecutor(client)
            await self._loop(stop_event)

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
        """Un ciclo: refresca cartera, detecta salidas (tiempo + take-profit), y liquida."""
        await self._poller.sync_once()
        now = _naive_utc_now()
        with get_session() as s:
            positions = list(s.exec(select(PortfolioPosition)))
        # DIAG: por qué dispara (o no) este tick — evita el silencio total cuando nada es debido.
        logger.info(f"[MOTOR 3 DIAG] {summarize_exits(positions, now).one_line()}")
        due = detect_and_log(positions, now)  # SHADOW: siempre loguea las salidas por tiempo

        # FASE 1 — Take-profit por precio. Caché de orderbook por ticker dentro del tick (no
        # re-pedir si una posición ya fue consultada). Unión deduplicada por ticker con `due`
        # para no doble-salir (el executor además tiene lock por-ticker como segunda red).
        bid_cache: dict[str, int | None] = {}
        exits: dict[str, PortfolioPosition] = {p.ticker: p for p in due}
        if self._take_profit_enabled:
            for p in positions:
                bid = await self._current_bid(p, bid_cache)
                if take_profit_due(p, bid, self._tp_threshold):
                    logger.info(
                        f"[MOTOR 3 TP SHADOW] take_profit {p.ticker} {p.count}c "
                        f"side={p.side} bid={bid}c >= {self._tp_threshold}c"
                    )
                    exits.setdefault(p.ticker, p)

        if self._executor is not None:
            for position in exits.values():
                await self._executor.exit_position(position)

    async def _current_bid(
        self, position: PortfolioPosition, cache: dict[str, int | None]
    ) -> int | None:
        """Bid actual del lado abierto (a quién le venderíamos). Cacheado por ticker dentro del
        tick. Fail-safe (Lección 7): un error de orderbook loguea y devuelve None — no rompe el
        tick ni dispara la salida (take_profit_due trata None como 'no decidible')."""
        if position.ticker in cache:
            return cache[position.ticker]
        bid: int | None = None
        if self._client is not None:
            try:
                ob = await self._client.get_orderbook(position.ticker)
                book = ob.get("orderbook", ob) if isinstance(ob, dict) else {}
                bid, _ = _top_bid(book.get(position.side) or [])
            except Exception as exc:
                logger.warning(f"motor3.tp.orderbook_error ticker={position.ticker}: {exc}")
                bid = None
        cache[position.ticker] = bid
        return bid
