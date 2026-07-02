"""
Motor5Engine — loop supervisado del market maker. DOS modos por construcción:
  - F1 SHADOW (trading_enabled=False, default): cotiza HIPOTÉTICAMENTE y registra.
  - F2 LIVE-demo (trading_enabled=True): además sincroniza quotes REALES vía
    Motor5Executor + MMReconciler (Capa A: solo se construyen en run() con el flag).

Flujo por tick (LOOP_INTERVAL_SEC):
  1. Consume FairValueBook.fresh(TTL) — sin fair fresco no hay universo (skip implícito;
     el gap fair_fresh=0 sostenido se ve en mm_funnel_snapshots).
  2. Por ticker (cap MOTOR_MM_MAX_TICKERS, orden determinístico): top-of-book vía REST
     get_orderbook (fallback del plan §7 — migrar a V2 cuando esté estable es un cambio
     local en _book_top). Sin book → skip_no_book (el MM nunca cotiza a ciegas).
  3. Fills hipotéticos: el book ACTUAL contra la quote resting del tick ANTERIOR
     (regla conservadora de cruce estricto, shadow_fill.py). Se aplican al inventario
     simulado y se persisten (mm_shadow_fills).
  4. Nueva quote (quoter.py) → persiste (mm_quotes) y queda resting para el próximo tick.
  5. MMFunnelSnapshot por tick + log una línea `motor5.funnel`.

En F2 el tick antepone los GATES: kill-switch (cancel-all una vez, luego gestión mínima),
quotes_paused (gestiona sin cotizar), reconcile (la VERDAD runtime; propaga/limpia
corrupción del executor) y la aplicación idempotente de fills reales al inventario.
Los fills SHADOW (inferencia por cruce) quedan apagados con executor: la única fuente
de fills en live es la verdad del reconciler/cancel-response. En producción el validador
de config bloquea EXECUTION=true hasta F3.

Regla de oro (Lección 9): el estado (inventario + quotes vivas) lo muta SOLO este loop,
secuencialmente. Una excepción aplicando estado de un ticker descarta su quote viva
(re-sync natural el próximo tick) — nunca "sigue operando" con estado dudoso.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from loguru import logger
from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.storage.models import (
    MMFunnelSnapshot,
    MMQuote,
    MMShadowFill,
    Trade,
    get_session,
    kill_switch_engaged,
    mm_quotes_paused,
)
from src.strategies.data_capture import _top_bid
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.executor import STRATEGY, Motor5Executor
from src.strategies.motor_5_mm.fill_feed import MMFillFeed
from src.strategies.motor_5_mm.inventory import InventoryBook
from src.strategies.motor_5_mm.quoter import QuoteSet, compute_quote
from src.strategies.motor_5_mm.reconciler import MMReconciler
from src.strategies.motor_5_mm.shadow_fill import ShadowFill, fills_for_quote


class Motor5Engine:
    """Market maker F1: quotes shadow alrededor del fair de Motor 2, contra el book real."""

    LOOP_INTERVAL_SEC = 60.0

    def __init__(
        self,
        *,
        max_tickers: int = 10,
        half_spread_cents: int = 3,
        quote_size_contracts: int = 10,
        max_inventory_contracts: int = 50,
        fair_ttl_sec: float = 600.0,
        client_factory: type[KalshiRestClient] = KalshiRestClient,
        trading_enabled: bool = False,
        risk_manager: RiskManager | None = None,
        fill_feed: MMFillFeed | None = None,
        mm_exposure_cap_usd: float | None = None,
    ) -> None:
        self._max_tickers = max_tickers
        self._half_spread = half_spread_cents
        self._size = quote_size_contracts
        self._max_inventory = max_inventory_contracts
        self._fair_ttl = fair_ttl_sec
        self._client_factory = client_factory
        self._client: KalshiRestClient | None = None
        self._inventory = InventoryBook()
        self._live_quotes: dict[str, QuoteSet] = {}
        self._last_marks: dict[str, float] = {}  # último mark conocido por ticker (MTM)
        # ── F2 (demo): ejecución real. CAPA A: el executor SOLO se construye (en run())
        # con trading_enabled=True — que el runner deriva de TRADING_ENABLED AND
        # MOTOR_MM_EXECUTION_ENABLED (y el validador de config bloquea EXECUTION=true en
        # producción hasta F3). Con executor=None, este engine ES el F1 shadow intacto.
        self._trading_enabled = trading_enabled
        self._risk = risk_manager
        self._fill_feed = fill_feed
        self._mm_exposure_cap_usd = mm_exposure_cap_usd
        self._executor: Motor5Executor | None = None
        self._reconciler: MMReconciler | None = None
        self._settled_coids: set[str] = set()  # fills ya aplicados al inventario (1 sola vez)
        self._kill_cancelled = False  # cancel-all del kill-switch ya disparado

    async def run(self, stop_event: asyncio.Event) -> None:
        mode = "F2 LIVE (demo)" if self._trading_enabled else "F1 SHADOW — CERO órdenes"
        logger.info(
            f"[MOTOR 5] arrancado {mode}. "
            f"max_tickers={self._max_tickers} half_spread={self._half_spread}c "
            f"size={self._size} max_inv={self._max_inventory} fair_ttl={self._fair_ttl}s"
        )
        async with self._client_factory() as client:
            self._client = client
            if self._trading_enabled:
                self._executor = Motor5Executor(
                    client, risk=self._risk, max_exposure_usd=self._mm_exposure_cap_usd
                )
                self._reconciler = MMReconciler(client)
            while not stop_event.is_set():
                try:
                    await self._tick()
                except Exception as exc:
                    logger.exception("motor5.engine.tick_failed")
                    BotState.record_error(f"motor5.engine: {type(exc).__name__}: {exc}")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=self.LOOP_INTERVAL_SEC)
        logger.info("[MOTOR 5 SHADOW] detenido (stop_event)")

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        counters = {
            "quoted": 0,
            "skip_no_book": 0,
            "skip_unprofitable": 0,
            "skip_degenerate": 0,
            "skip_fair_range": 0,
            "fills": 0,
            "exec_corrupted": 0,
            "exec_risk_blocked": 0,
        }
        # ── Gates F2 (solo con executor) — se evalúan ANTES de cualquier quote ──────
        quotes_allowed = True
        if self._executor is not None:
            engaged, ks_reason = kill_switch_engaged()
            if engaged:
                # Pánico: cancel-all UNA vez (<5s vía batch) y el motor queda en gestión
                # mínima (reconcile) sin cotizar hasta que el humano limpie el switch.
                if not self._kill_cancelled:
                    n = await self._executor.cancel_all(f"kill_switch: {ks_reason}")
                    logger.warning(f"[MOTOR 5] kill-switch → cancel_all n={n}")
                    self._kill_cancelled = True
                quotes_allowed = False
            else:
                self._kill_cancelled = False
                paused, pause_reason = mm_quotes_paused()
                if paused:
                    # quotes_paused: NO se emiten quotes nuevas; se sigue gestionando
                    # (reconcile + fills). Las resting existentes quedan gestionadas.
                    logger.info(f"[MOTOR 5] quotes_paused ({pause_reason}) → tick sin cotizar")
                    quotes_allowed = False
            # Fast-path del canal fill: cualquier fill encolado dispara reconcile YA.
            ws_fills = self._fill_feed.drain() if self._fill_feed is not None else []
            if self._reconciler is not None:
                report = await self._reconciler.reconcile()
                # Verdad del reconcile: los tickers con discrepancia quedan corruptos;
                # los que salieron limpios se des-corrompen (ya se puede cotizar).
                self._executor.corrupted = set(report.corrupted_tickers)
                if ws_fills:
                    logger.info(f"[MOTOR 5] fill_feed fast-path: {len(ws_fills)} fills")
            counters["fills"] += self._apply_settled_fills()
        fairs = FairValueBook.fresh(self._fair_ttl, now=now)
        # Universo del tick: determinístico (orden alfabético) y capado. Los tickers que
        # SALEN del universo retiran su quote viva (cancel hipotético en shadow; cancel
        # REAL con executor — dejarla resting sería una quote sin fair que la respalde).
        tickers = sorted(fairs)[: self._max_tickers] if quotes_allowed else []
        for stale_ticker in [t for t in self._live_quotes if t not in tickers]:
            del self._live_quotes[stale_ticker]
            if self._executor is not None:
                await self._executor.retire_ticker(stale_ticker)
        for ticker in tickers:
            fv = fairs[ticker]
            top = await self._book_top(ticker)
            if top is None:
                counters["skip_no_book"] += 1
                # Sin book no se evalúan fills (no hay evidencia de cruce) NI se re-cotiza:
                # la quote vieja se retira (no cotizamos a ciegas — plan §7, books stale).
                self._live_quotes.pop(ticker, None)
                if self._executor is not None:
                    await self._executor.retire_ticker(ticker)
                continue
            yes_bid, yes_ask = top
            self._record_mark(ticker, yes_bid, yes_ask, fv.fair_prob)
            # Fills SHADOW solo sin executor (F1): con ejecución real, los fills vienen
            # de la verdad del reconciler (_apply_settled_fills) — nunca de la inferencia.
            prev = self._live_quotes.get(ticker)
            if prev is not None and self._executor is None:
                try:
                    counters["fills"] += self._settle_fills(prev, yes_bid, yes_ask)
                except Exception:
                    # Estado del ticker en duda → quote fuera y re-sync el próximo tick.
                    self._live_quotes.pop(ticker, None)
                    raise
            quote, skip = compute_quote(
                ticker,
                fv.fair_prob,
                half_spread_cents=self._half_spread,
                size_contracts=self._size,
                inventory_contracts=self._inventory.net(ticker),
                max_inventory_contracts=self._max_inventory,
                best_yes_bid=yes_bid,
                best_yes_ask=yes_ask,
            )
            if quote is None:
                key = {"fair_out_of_range": "skip_fair_range"}.get(skip or "", f"skip_{skip}")
                counters[key] = counters.get(key, 0) + 1
                self._live_quotes.pop(ticker, None)
                if self._executor is not None:
                    await self._executor.retire_ticker(ticker)
                continue
            counters["quoted"] += 1
            self._live_quotes[ticker] = quote
            self._persist_quote(
                quote,
                fv_age_sec=(now - fv.computed_at).total_seconds(),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
            )
            # CAPA B: guard en el call site — sin executor (shadow) acá no pasa nada.
            if self._executor is not None:
                outcome = await self._executor.sync_quotes(quote)
                if outcome == "corrupted":
                    counters["exec_corrupted"] += 1
                elif outcome == "risk_blocked":
                    counters["exec_risk_blocked"] += 1
        mtm = self._inventory.total_mtm_cents(self._last_marks)
        self._persist_snapshot(len(fairs), counters, mtm)
        logger.info(
            f"motor5.funnel fair_fresh={len(fairs)} quoted={counters['quoted']} "
            f"skip_book={counters['skip_no_book']} skip_unprof={counters['skip_unprofitable']} "
            f"skip_degen={counters['skip_degenerate']} skip_fair={counters['skip_fair_range']} "
            f"fills={counters['fills']} inv_abs={self._inventory.total_abs_contracts()} "
            f"mtm={mtm}c"
            + (
                f" exec[corrupted={counters['exec_corrupted']} "
                f"risk_blocked={counters['exec_risk_blocked']}]"
                if self._executor is not None
                else ""
            )
        )

    def _apply_settled_fills(self) -> int:
        """Aplica al inventario los fills REALES ya confirmados (filas Trade filled del
        motor), UNA sola vez por client_order_id. Única fuente de mutación del inventario
        en modo live: la verdad del reconciler/cancel-response — jamás la inferencia del
        shadow ni el WS directo (evita doble conteo y carreras)."""
        try:
            with get_session() as s:
                rows = list(
                    s.exec(
                        select(Trade).where(Trade.strategy == STRATEGY, Trade.status == "filled")
                    )
                )
        except Exception:
            logger.exception("motor5.engine.settled_fills_query_error")
            return 0
        applied = 0
        for row in rows:
            if row.client_order_id in self._settled_coids:
                continue
            self._settled_coids.add(row.client_order_id)
            count = row.filled_count if row.filled_count is not None else row.count
            if count <= 0:
                continue
            fill = ShadowFill(
                ticker=row.ticker,
                side=row.action,  # buy (bid) | sell (ask), eje YES
                price_cents=row.fill_price_cents or row.price_cents,
                count=count,
                rule="real_fill",
            )
            inv = self._inventory.apply_fill(fill)
            applied += 1
            logger.info(
                f"[MOTOR 5 LIVE] fill {fill.side} {count}x{row.ticker} "
                f"@{fill.price_cents}c net={inv.net_contracts} fees={inv.fees_cents}c"
            )
        return applied

    def _settle_fills(self, quote: QuoteSet, yes_bid: int | None, yes_ask: int | None) -> int:
        """Aplica los fills hipotéticos de la quote resting contra el book actual."""
        fills = fills_for_quote(quote, best_yes_bid=yes_bid, best_yes_ask=yes_ask)
        for fill in fills:
            inv = self._inventory.apply_fill(fill)
            logger.info(
                f"[MOTOR 5 SHADOW] fill {fill.side} {fill.count}x{fill.ticker} "
                f"@{fill.price_cents}c ({fill.rule}) net={inv.net_contracts} "
                f"cash={inv.cash_cents}c fees={inv.fees_cents}c"
            )
            self._persist_fill(fill, inv.net_contracts)
        return len(fills)

    def _record_mark(
        self, ticker: str, yes_bid: int | None, yes_ask: int | None, fair_prob: float
    ) -> None:
        """Mark para el MTM: mid del book si hay dos lados; si no, el fair (mejor prior)."""
        if yes_bid is not None and yes_ask is not None:
            self._last_marks[ticker] = (yes_bid + yes_ask) / 2.0
        else:
            self._last_marks[ticker] = fair_prob * 100.0

    async def _book_top(self, ticker: str) -> tuple[int | None, int | None] | None:
        """Top-of-book YES vía REST. (yes_bid, yes_ask); None = sin book usable.

        El book de Kalshi lista BIDs resting de cada lado: yes_ask = 100 − no_bid (todo se
        cotiza desde el eje YES). Fail-safe (Lección 7): error → None, el tick sigue."""
        if self._client is None:
            return None
        try:
            ob = await self._client.get_orderbook(ticker)
        except Exception as exc:
            logger.warning(f"motor5.book_error ticker={ticker}: {type(exc).__name__}: {exc}")
            return None
        book = ob.get("orderbook", ob) if isinstance(ob, dict) else {}
        yes_bid, _ = _top_bid(book.get("yes") or [])
        no_bid, _ = _top_bid(book.get("no") or [])
        yes_ask = (100 - no_bid) if no_bid is not None else None
        if yes_bid is None and yes_ask is None:
            return None
        return yes_bid, yes_ask

    # ---- persistencia best-effort (un fallo de DB loguea, no rompe el tick) ----

    def _persist_quote(
        self, quote: QuoteSet, *, fv_age_sec: float, yes_bid: int | None, yes_ask: int | None
    ) -> None:
        try:
            with get_session() as s:
                s.add(
                    MMQuote(
                        ticker=quote.ticker[:100],
                        fair_prob=round(quote.fair_prob, 4),
                        fair_age_sec=round(fv_age_sec, 1),
                        bid_cents=quote.bid_cents,
                        ask_cents=quote.ask_cents,
                        size=quote.size,
                        yes_bid=yes_bid,
                        yes_ask=yes_ask,
                        inventory=self._inventory.net(quote.ticker),
                    )
                )
                s.commit()
        except Exception:
            logger.exception("motor5.persist_quote_error")

    def _persist_fill(self, fill, inventory_after: int) -> None:
        from src.math.fees import kalshi_fee_cents

        try:
            with get_session() as s:
                s.add(
                    MMShadowFill(
                        ticker=fill.ticker[:100],
                        side=fill.side,
                        price_cents=fill.price_cents,
                        count=fill.count,
                        fee_cents=kalshi_fee_cents(fill.count, fill.price_cents),
                        rule=fill.rule[:50],
                        inventory_after=inventory_after,
                    )
                )
                s.commit()
        except Exception:
            logger.exception("motor5.persist_fill_error")

    def _persist_snapshot(self, fair_fresh: int, counters: dict[str, int], mtm: int) -> None:
        try:
            with get_session() as s:
                s.add(
                    MMFunnelSnapshot(
                        fair_fresh=fair_fresh,
                        quoted=counters["quoted"],
                        skip_no_book=counters["skip_no_book"],
                        skip_unprofitable=counters["skip_unprofitable"],
                        skip_degenerate=counters["skip_degenerate"],
                        skip_fair_range=counters["skip_fair_range"],
                        fills=counters["fills"],
                        inventory_abs=self._inventory.total_abs_contracts(),
                        mtm_pnl_cents=mtm,
                    )
                )
                s.commit()
        except Exception:
            logger.exception("motor5.persist_snapshot_error")
