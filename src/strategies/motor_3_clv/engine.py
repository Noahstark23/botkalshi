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
from sqlmodel import col, select

from src.clients.kalshi_rest import KalshiRestClient
from src.math.fees import kalshi_fee_cents
from src.monitoring.health import BotState
from src.storage.models import PortfolioPosition, Trade, _naive_utc_now, get_session
from src.strategies.data_capture import _top_bid
from src.strategies.motor_3_clv.detector import detect_and_log, summarize_exits
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor
from src.strategies.motor_3_clv.poller import PortfolioPoller
from src.strategies.motor_3_clv.take_profit import DEFAULT_TAKE_PROFIT_CENTS, take_profit_due
from src.strategies.motor_3_clv.trailing_stop import (
    DEFAULT_TRAILING_DROP_CENTS,
    next_peak_bid,
    trailing_stop_due,
)

# Estrategias de las patas BUY que un exit cierra (= _settle_originals). El entry del trailing
# se deriva del primer BUY filled de estas (FIFO).
# ⚠️ motor_rest_arb EXCLUIDO (fix auditoría 2026-07-01): una posición respaldada por una leg
# de arb hedged NUNCA se liquida suelta — vender la leg ganadora (TP a bid≥90 o salida T-30)
# rompe el hedge y convierte profit lockeado en pérdida realizada. Esas posiciones las
# resuelve el SettlementPoller del Motor REST por resolución de mercado.
_ENTRY_ORIGIN = ("motor_2_consensus",)


class Motor3Engine:
    """Bucle CLV: sincroniza cartera → detecta salidas a T-30min → (liquida si trading on)."""

    LOOP_INTERVAL_SEC = 60.0

    def __init__(
        self,
        *,
        trading_enabled: bool = False,
        take_profit_enabled: bool = False,
        tp_threshold: int = DEFAULT_TAKE_PROFIT_CENTS,
        trailing_enabled: bool = False,
        trailing_drop: int = DEFAULT_TRAILING_DROP_CENTS,
    ) -> None:
        self._poller = PortfolioPoller()
        self._trading_enabled = trading_enabled
        self._take_profit_enabled = take_profit_enabled
        self._tp_threshold = tp_threshold
        self._trailing_enabled = trailing_enabled
        self._trailing_drop = trailing_drop
        self._executor: Motor3ExitExecutor | None = None
        self._client: KalshiRestClient | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            f"motor3.engine started (trading_enabled={self._trading_enabled} "
            f"take_profit_enabled={self._take_profit_enabled} tp_threshold={self._tp_threshold}c "
            f"trailing_enabled={self._trailing_enabled} trailing_drop={self._trailing_drop}c)"
        )
        # El cliente REST se abre SIEMPRE: el take-profit necesita leer el orderbook
        # (bid del lado abierto) aun en shadow. La venta sigue gateada por la EXISTENCIA del
        # executor (CAPA A): solo se construye con trading on → en shadow lee pero jamás vende.
        async with KalshiRestClient() as client:
            self._client = client
            if self._trading_enabled:
                self._executor = Motor3ExitExecutor(client, entry_origin=_ENTRY_ORIGIN)
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
        # ATRIBUCIÓN (fix auditoría 2026-07-01): PortfolioPosition es la posición NETA de la
        # CUENTA — no distingue qué motor la abrió. Motor 3 solo gestiona posiciones
        # respaldadas por BUYs abiertos de _ENTRY_ORIGIN; una leg de arb REST o una posición
        # manual del dueño NO se toca (ni siquiera en shadow logs — no son salidas nuestras).
        positions = self._attributable_positions(positions)
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
                    entry = self._entry_bid_for(p)
                    logger.info(
                        f"[MOTOR 3 TP SHADOW] take_profit {p.ticker} {p.count}c "
                        f"side={p.side} bid={bid}c >= {self._tp_threshold}c "
                        f"{self._shadow_pnl_str(p, bid, entry)}"
                    )
                    exits.setdefault(p.ticker, p)

        # FASE 2 — Trailing stop. Reusa bid_cache y el dict `exits` (mismo dedup por ticker que
        # TP/tiempo). Solo se arma como protección de ganancia: el entry sale de la pata BUY
        # (None → no se arma). El peak se persiste entre ticks. Fail-safe por posición.
        if self._trailing_enabled:
            for p in positions:
                entry = self._entry_bid_for(p)
                if entry is None:
                    continue
                bid = await self._current_bid(p, bid_cache)
                if bid is None:
                    continue
                peak = next_peak_bid(p.peak_bid_cents, bid, entry)
                if peak != p.peak_bid_cents:
                    self._persist_peak(p, peak)
                if trailing_stop_due(p, peak, bid, entry, self._trailing_drop):
                    logger.info(
                        f"[MOTOR 3 TRAIL SHADOW] {p.ticker} {p.count}c side={p.side} "
                        f"peak={peak}c bid={bid}c entry={entry}c drop={self._trailing_drop}c "
                        f"-> cerraría {self._shadow_pnl_str(p, bid, entry)}"
                    )
                    exits.setdefault(p.ticker, p)

        if self._executor is not None:
            for position in exits.values():
                await self._executor.exit_position(position)

    def _attributable_positions(
        self, positions: list[PortfolioPosition]
    ) -> list[PortfolioPosition]:
        """Filtra a las posiciones que Motor 3 tiene derecho a gestionar.

        Reglas (fix auditoría 2026-07-01 — PortfolioPosition es la posición NETA de la
        cuenta, sin atribución de origen):
          - Ticker con CUALQUIER pata abierta de motor_rest_arb (cualquier side) → SKIP:
            es (parte de) un arb hedged; venderla suelta rompe el hedge.
          - (ticker, side) sin ningún BUY filled abierto de _ENTRY_ORIGIN → SKIP: posición
            manual del dueño o de origen desconocido — Motor 3 no la abrió, no la vende.
        Fail-safe: error de DB → lista vacía (ante la duda, este tick no gestiona nada).
        """
        if not positions:
            return positions
        try:
            with get_session() as s:
                open_buys = list(
                    s.exec(
                        select(Trade).where(
                            Trade.action == "buy",
                            Trade.status == "filled",
                            col(Trade.closed_by_clv).is_(False),
                        )
                    )
                )
        except Exception as exc:
            logger.warning(f"motor3.attribution.load_error: {exc} → tick sin posiciones")
            return []
        rest_tickers = {b.ticker for b in open_buys if b.strategy == "motor_rest_arb"}
        attributable = {
            (b.ticker, b.side) for b in open_buys if b.strategy in _ENTRY_ORIGIN and b.count > 0
        }
        kept: list[PortfolioPosition] = []
        skipped_rest, skipped_foreign = 0, 0
        for p in positions:
            if p.ticker in rest_tickers:
                skipped_rest += 1
                continue
            if (p.ticker, p.side) not in attributable:
                skipped_foreign += 1
                continue
            kept.append(p)
        if skipped_rest or skipped_foreign:
            logger.info(
                f"motor3.attribution kept={len(kept)} skip_rest_arb={skipped_rest} "
                f"skip_foreign={skipped_foreign} (legs de arb hedged y posiciones ajenas no se gestionan)"
            )
        return kept

    def _entry_bid_for(self, position: PortfolioPosition) -> int | None:
        """Entry del lado abierto = primer BUY filled (FIFO por placed_at), igual criterio que
        _settle_originals. None si no hay pata BUY (→ el trailing no se arma; fail-safe). Best-
        effort: un fallo de DB loguea y devuelve None (no rompe el tick — Lección 7)."""
        try:
            with get_session() as s:
                buy = s.exec(
                    select(Trade)
                    .where(
                        Trade.ticker == position.ticker,
                        Trade.side == position.side,
                        Trade.action == "buy",
                        Trade.status == "filled",
                        col(Trade.strategy).in_(_ENTRY_ORIGIN),
                    )
                    .order_by(col(Trade.placed_at))
                ).first()
        except Exception as exc:
            logger.warning(f"motor3.trail.entry_error ticker={position.ticker}: {exc}")
            return None
        if buy is None:
            return None
        return buy.fill_price_cents or buy.price_cents

    def _shadow_pnl_str(self, position: PortfolioPosition, bid: int, entry: int | None) -> str:
        """PnL realizado NETO de fees si la posición se cerrara al bid ahora — para validar el
        shadow contra el backtest 'contando fees reales' antes de prender la venta. Usa el entry
        de la pata BUY (FIFO) y kalshi_fee_cents en ambos lados (igual que _settle_originals).
        Si no hay entry, no se puede calcular."""
        if entry is None:
            return "entry=? net_pnl=?"
        gross_cents = position.count * (bid - entry)
        fees_cents = kalshi_fee_cents(position.count, bid) + kalshi_fee_cents(position.count, entry)
        net_cents = gross_cents - fees_cents
        return (
            f"entry={entry}c gross=${gross_cents / 100:+.2f} "
            f"fees=${fees_cents / 100:.2f} net=${net_cents / 100:+.2f}"
        )

    def _persist_peak(self, position: PortfolioPosition, peak: int) -> None:
        """Persiste el nuevo pico en portfolio_positions (UPDATE por ticker, único). Refleja el
        valor en el objeto en memoria del tick. Best-effort: un fallo loguea y sigue."""
        try:
            with get_session() as s:
                row = s.exec(
                    select(PortfolioPosition).where(PortfolioPosition.ticker == position.ticker)
                ).first()
                if row is not None:
                    row.peak_bid_cents = peak
                    s.add(row)
                    s.commit()
            position.peak_bid_cents = peak
        except Exception as exc:
            logger.warning(f"motor3.trail.persist_peak_error ticker={position.ticker}: {exc}")

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
