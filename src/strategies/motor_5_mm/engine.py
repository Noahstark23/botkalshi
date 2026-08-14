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
import time
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
from src.strategies.data_capture import _top_bid, rest_orderbook_sides
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
    # Re-arme del diagnóstico de book: un skip CRÓNICO no debe auto-silenciarse — un
    # one-shot de por vida se consume una vez (p.ej. en el boot, fuera de la ventana de
    # log que alguien esté mirando) y el skip perpetuo vuelve a ser arqueología.
    BOOK_DIAG_REARM_SEC = 1800.0
    # FRESCURA del mark para medir markout (bug encontrado por la sonda 2026-08-14):
    # _last_marks NUNCA se invalida — con max_tickers=10 y fair_fresh=20-48 los tickers
    # ROTAN fuera del universo, el mark se congela y AMBOS horizontes medían contra el
    # mismo valor viejo (markout2 == markout1 exacto en 5/5 fills). El T+5min —
    # justamente el que detecta selección adversa SOSTENIDA — estaba ciego. Un markout
    # contra un mark congelado no es una medición: se descarta y se espera uno fresco.
    MARK_FRESH_MAX_SEC = 90.0  # 1.5 ticks

    def __init__(
        self,
        *,
        max_tickers: int = 10,
        half_spread_cents: int = 3,
        edge_skew_cents: int = 0,
        quote_size_contracts: int = 10,
        max_inventory_contracts: int = 50,
        fair_ttl_sec: float = 600.0,
        client_factory: type[KalshiRestClient] = KalshiRestClient,
        trading_enabled: bool = False,
        risk_manager: RiskManager | None = None,
        fill_feed: MMFillFeed | None = None,
        mm_exposure_cap_usd: float | None = None,
        fees_as_maker: bool = False,
        jump_retreat_cents: float = 5.0,
    ) -> None:
        self._max_tickers = max_tickers
        self._half_spread = half_spread_cents
        self._edge_skew = edge_skew_cents
        self._size = quote_size_contracts
        self._max_inventory = max_inventory_contracts
        self._fair_ttl = fair_ttl_sec
        self._client_factory = client_factory
        self._client: KalshiRestClient | None = None
        # APUESTA 1 (2026-08-12): modelo de fee del shadow — maker ($0) o taker (legacy).
        self._fees_as_maker = fees_as_maker
        # Retiro por salto (blindaje del maker): 0 = off.
        self._jump_retreat = jump_retreat_cents
        self._inventory = InventoryBook(fees_as_maker=fees_as_maker)
        # MARKOUT (la métrica que decide si el maker chico sobrevive): fills shadow
        # pendientes de medir el mid a T+30s y T+5min contra el precio del fill —
        # markout negativo sistemático = selección adversa (nos cruzan cuando el fair
        # ya se movió). Acotado (nada sin tope) y best-effort.
        self._markouts_pendientes: list[dict] = []
        self._live_quotes: dict[str, QuoteSet] = {}
        self._last_marks: dict[str, float] = {}  # último mark conocido por ticker (MTM)
        self._last_marks_at: dict[str, float] = {}  # monotonic del último refresh del mark
        # Salto del mark en el tick en que se CREÓ cada quote viva. El fill lo hereda
        # (quote_jump_cents) → permite evaluar CUALQUIER umbral de blindaje después,
        # no solo el configurado. ticker → salto en ¢ (0.0 si el tick fue calmo).
        self._quote_jump: dict[str, float] = {}
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
        # Diagnóstico de book POR-TICKER con backoff (no un bool global de por vida): un
        # ticker sano en el boot no consume el diagnóstico del que tiene el problema, y
        # un skip crónico re-loguea cada BOOK_DIAG_REARM_SEC en vez de callarse para
        # siempre tras la primera vez. Valor = time.monotonic() del último log.
        self._book_diag_last: dict[str, float] = {}

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
            # Shutdown limpio (auditoría 2026-07-07, P0 de F2): sin esto las quotes GTC
            # quedaban RESTING en el book sin bot que las gestione — un fill contra una
            # quote huérfana durante el redeploy es exposición que nadie ve hasta el
            # próximo boot. Best-effort: un fallo acá no bloquea el shutdown (el
            # reconciler del próximo boot cancela lo que sobreviva como huérfana).
            if self._executor is not None:
                try:
                    n = await self._executor.cancel_all("shutdown")
                    logger.info(f"[MOTOR 5] shutdown → cancel_all n={n}")
                except Exception:
                    logger.exception("motor5.engine.shutdown_cancel_error")
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
                # live_coids: lo que ESTE proceso gestiona. Tras un restart es vacío →
                # el reconcile cancela las resting del proceso anterior (huérfanas, P0
                # auditoría 2026-07-07) antes de que el tick cotice encima.
                report = await self._reconciler.reconcile(live_coids=self._executor.live_coids())
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
            # SALTO del mark desde el tick anterior (Apuesta 1, blindaje): el insumo del
            # retiro por salto Y la etiqueta de los fills (mark_jump_cents). Se captura
            # ANTES de refrescar el mark.
            mark_previo = self._last_marks.get(ticker)
            self._record_mark(ticker, yes_bid, yes_ask, fv.fair_prob)
            salto = abs(self._last_marks[ticker] - mark_previo) if mark_previo is not None else None
            # Fills SHADOW solo sin executor (F1): con ejecución real, los fills vienen
            # de la verdad del reconciler (_apply_settled_fills) — nunca de la inferencia.
            prev = self._live_quotes.get(ticker)
            if prev is not None and self._executor is None:
                try:
                    # El fill que un salto ya causó SE CUENTA (un maker real tampoco
                    # cancela en 0ms) — pero queda etiquetado para que el gate segmente
                    # markout de salto vs calmo. Medir, no asumir.
                    counters["fills"] += self._settle_fills(
                        prev,
                        yes_bid,
                        yes_ask,
                        fill_fair_prob=fv.fair_prob,
                        mark_jump=salto,
                        quote_jump=self._quote_jump.get(ticker),
                    )
                except Exception:
                    # Estado del ticker en duda → quote fuera y re-sync el próximo tick.
                    self._live_quotes.pop(ticker, None)
                    raise
            # RETIRO POR SALTO (primer dato del gate 13-ago: 4 fills in-play con markout
            # −18/−20¢, ambos lados perdiendo a la vez — el evento del juego atravesó
            # las quotes): mark saltó ≥ umbral → la quote se retira y este tick no se
            # re-cotiza. Vuelve sola al primer tick calmo.
            if salto is not None and self._jump_retreat > 0 and salto >= self._jump_retreat:
                counters["skip_jump"] = counters.get("skip_jump", 0) + 1
                if self._executor is not None:
                    # LIVE: retirar de VERDAD — acá el blindaje protege plata real.
                    self._live_quotes.pop(ticker, None)
                    self._quote_jump.pop(ticker, None)
                    await self._executor.retire_ticker(ticker)
                    logger.info(
                        f"motor5.jump_retreat ticker={ticker} salto={salto:.1f}c "
                        f"≥ {self._jump_retreat:.0f}c → quote RETIRADA (live)"
                    )
                    continue
                # SHADOW (2026-08-14): NO se retira. Retirar acá protege $0 y CUESTA
                # DATOS — el gate se quedaría sin los fills que necesita medir (n=9 en
                # dos días; a ese ritmo no hay veredicto). La quote sigue viva, el fill
                # se mide, y hereda el salto de su tick de creación (quote_jump_cents):
                # con eso el análisis reconstruye la política de CUALQUIER umbral
                # (blindaje@5 = subconjunto con quote_jump<5) desde los mismos datos.
                # El shadow MIDE, el live PROTEGE.
                logger.info(
                    f"motor5.jump_flag ticker={ticker} salto={salto:.1f}c "
                    f"≥ {self._jump_retreat:.0f}c → el blindaje HABRÍA retirado "
                    "(shadow: se mide igual y se etiqueta)"
                )
            quote, skip = compute_quote(
                ticker,
                fv.fair_prob,
                half_spread_cents=self._half_spread,
                edge_skew_cents=self._edge_skew,
                size_contracts=self._size,
                inventory_contracts=self._inventory.net(ticker),
                max_inventory_contracts=self._max_inventory,
                best_yes_bid=yes_bid,
                best_yes_ask=yes_ask,
                fees_as_maker=self._fees_as_maker,
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
            self._quote_jump[ticker] = salto if salto is not None else 0.0
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
        # MARKOUT: con los marks del tick recién refrescados, medir los fills pendientes
        # (T+30s / T+5min). Va acá y no en _settle_fills porque el mark "posterior" de un
        # fill es el de un tick FUTURO, no el del tick que lo detectó.
        self._medir_markouts()
        mtm = self._inventory.total_mtm_cents(self._last_marks)
        self._persist_snapshot(len(fairs), counters, mtm)
        logger.info(
            f"motor5.funnel fair_fresh={len(fairs)} quoted={counters['quoted']} "
            f"skip_book={counters['skip_no_book']} skip_unprof={counters['skip_unprofitable']} "
            f"skip_degen={counters['skip_degenerate']} skip_fair={counters['skip_fair_range']} "
            f"skip_jump={counters.get('skip_jump', 0)} "
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

    def _settle_fills(
        self,
        quote: QuoteSet,
        yes_bid: int | None,
        yes_ask: int | None,
        *,
        fill_fair_prob: float,
        mark_jump: float | None = None,
        quote_jump: float | None = None,
    ) -> int:
        """Aplica los fills hipotéticos de la quote resting contra el book actual.

        Fair-at-fill (2026-08-07): junto con el fill se registra el fair VIGENTE (el del
        ciclo que detectó el cruce) además del fair de la quote (tick t−1). La resta de
        ambos separa la pregunta del A/B: edge≥0 contra el fair vigente = spread capturado;
        edge<0 = el fair se movió y el mercado nos cruzó (selección adversa). El dato se
        persiste crudo (probs + book) y el juicio lo hace el análisis, no el hot path."""
        fills = fills_for_quote(quote, best_yes_bid=yes_bid, best_yes_ask=yes_ask)
        for fill in fills:
            inv = self._inventory.apply_fill(fill)
            fill_fair_cents = fill_fair_prob * 100.0
            edge_c = (
                fill_fair_cents - fill.price_cents
                if fill.side == "buy"
                else fill.price_cents - fill_fair_cents
            )
            drift_c = (fill_fair_prob - quote.fair_prob) * 100.0
            logger.info(
                f"[MOTOR 5 SHADOW] fill {fill.side} {fill.count}x{fill.ticker} "
                f"@{fill.price_cents}c ({fill.rule}) net={inv.net_contracts} "
                f"cash={inv.cash_cents}c fees={inv.fees_cents}c "
                f"edge={edge_c:+.1f}c drift={drift_c:+.1f}c"
            )
            fill_id = self._persist_fill(
                fill,
                inv.net_contracts,
                quote_fair_prob=quote.fair_prob,
                fill_fair_prob=fill_fair_prob,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                mark_jump=mark_jump,
                quote_jump=quote_jump,
            )
            if fill_id is not None:
                # Encolar la medición de markout (T+30s/T+5min contra el mark futuro).
                self._markouts_pendientes.append(
                    {
                        "id": fill_id,
                        "ticker": fill.ticker,
                        "side": fill.side,
                        "price": fill.price_cents,
                        "t_mono": time.monotonic(),
                        "m1_hecho": False,
                    }
                )
                if len(self._markouts_pendientes) > 500:  # nada sin tope
                    self._markouts_pendientes.pop(0)
        return len(fills)

    def _medir_markouts(self) -> None:
        """Markout de los fills shadow: (mark_posterior − precio) para buys, invertido
        para sells — positivo = el mercado nos dio la razón; negativo sistemático =
        SELECCIÓN ADVERSA (nos cruzan cuando el fair ya se movió), la causa de muerte
        documentada del maker chico. Resolución = cadencia del tick (~60s): el T+30s
        real es "el primer tick ≥30s" y se persiste la edad exacta. Best-effort."""
        if not self._markouts_pendientes:
            return
        ahora = time.monotonic()
        restantes: list[dict] = []
        for p in self._markouts_pendientes:
            edad = ahora - p["t_mono"]
            mark = self._mark_fresco(p["ticker"], ahora)
            try:
                if not p["m1_hecho"] and edad >= 30.0 and mark is not None:
                    self._persist_markout(p["id"], 1, self._markout_cents(p, mark), edad)
                    p["m1_hecho"] = True
                    # markout2 JAMÁS en la misma pasada: dos horizontes exigen DOS
                    # observaciones distintas del mercado, no la misma leída dos veces.
                elif edad >= 300.0:
                    if mark is not None:
                        self._persist_markout(p["id"], 2, self._markout_cents(p, mark), edad)
                        continue  # completo → sale de la cola
                if edad >= 600.0:
                    continue  # sin mark FRESCO en 10min (ticker fuera del universo) → soltar
            except Exception:
                logger.exception("motor5.markout_error")
                continue  # un fill problemático no bloquea la cola
            restantes.append(p)
        self._markouts_pendientes = restantes

    def _mark_fresco(self, ticker: str, ahora: float) -> float | None:
        """El mark SOLO si fue refrescado en este tick o el anterior. Un ticker que rotó
        fuera del universo conserva su último mark en _last_marks para siempre; medir
        contra él es inventar una observación que nadie hizo."""
        marcado_en = self._last_marks_at.get(ticker)
        if marcado_en is None or (ahora - marcado_en) > self.MARK_FRESH_MAX_SEC:
            return None
        return self._last_marks.get(ticker)

    @staticmethod
    def _markout_cents(p: dict, mark_cents: float) -> float:
        signo = 1.0 if p["side"] == "buy" else -1.0
        return signo * (mark_cents - p["price"])

    def _persist_markout(self, fill_id: int, which: int, valor: float, edad_sec: float) -> None:
        try:
            with get_session() as s:
                row = s.get(MMShadowFill, fill_id)
                if row is None:
                    return
                if which == 1:
                    row.markout1_cents = round(valor, 2)
                    row.markout1_age_sec = round(edad_sec, 1)
                else:
                    row.markout2_cents = round(valor, 2)
                    row.markout2_age_sec = round(edad_sec, 1)
                s.add(row)
                s.commit()
            logger.info(
                f"[MOTOR 5 SHADOW] markout fill_id={fill_id} t+{edad_sec:.0f}s "
                f"mo{which}={valor:+.1f}c"
            )
        except Exception:
            logger.exception("motor5.persist_markout_error")

    def _record_mark(
        self, ticker: str, yes_bid: int | None, yes_ask: int | None, fair_prob: float
    ) -> None:
        """Mark para el MTM: mid del book si hay dos lados; si no, el fair (mejor prior).
        Estampa el instante: el markout exige un mark FRESCO (ver MARK_FRESH_MAX_SEC)."""
        if yes_bid is not None and yes_ask is not None:
            self._last_marks[ticker] = (yes_bid + yes_ask) / 2.0
        else:
            self._last_marks[ticker] = fair_prob * 100.0
        self._last_marks_at[ticker] = time.monotonic()

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
        # Shape 2026-07-15: la API migró a 'orderbook_fp' + 'yes_dollars'/'no_dollars'
        # (dólares-string) y este parser leía vacío en silencio (160 líneas book_shape
        # sobre books con ~$500k resting). El normalizador tolera TODAS las generaciones.
        sides = rest_orderbook_sides(ob)
        yes_levels = sides["yes"]
        no_levels = sides["no"]
        yes_bid, _ = _top_bid(yes_levels)
        no_bid, _ = _top_bid(no_levels)
        yes_ask = (100 - no_bid) if no_bid is not None else None
        if yes_bid is None and yes_ask is None:
            # Diagnóstico POR-TICKER que separa las tres causas que el funnel colapsa en
            # 'skip_no_book' (el quoter tolera un solo lado; acá solo cae AMBOS sin top):
            #   a) listas presentes pero VACÍAS  → book sin resting (selección de market)
            #   b) niveles presentes pero size=0 → sin volumen usable
            #   c) claves inesperadas / no-lista → shape (book_keys lo delata)
            # OJO forense: si skip_no_book incrementa SIN ninguna línea book_shape, el None
            # salió del path de excepción de arriba → grep motor5.book_error, no shape.
            mono = time.monotonic()
            last = self._book_diag_last.get(ticker)
            if last is None or mono - last >= self.BOOK_DIAG_REARM_SEC:
                self._book_diag_last[ticker] = mono
                logger.warning(
                    f"motor5.book_shape ticker={ticker} sin top usable — "
                    f"yes_levels={len(yes_levels)} no_levels={len(no_levels)} "
                    f"resp_keys={sorted(ob.keys()) if isinstance(ob, dict) else type(ob).__name__} "
                    f"raw={str(ob)[:400]}"
                )
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

    def _persist_fill(
        self,
        fill,
        inventory_after: int,
        *,
        quote_fair_prob: float | None = None,
        fill_fair_prob: float | None = None,
        yes_bid: int | None = None,
        yes_ask: int | None = None,
        mark_jump: float | None = None,
        quote_jump: float | None = None,
    ) -> int | None:
        """Devuelve el id de la fila (para el markout) o None si la persistencia falló.
        fee_cents SIEMPRE registra la fee de TAKER (kalshi_fee_cents) aunque el modelo
        del shadow sea maker: es el dato que permite derivar las DOS contabilidades de
        la misma tabla — el análisis resta o no según el modelo bajo estudio."""
        from src.math.fees import kalshi_fee_cents, kalshi_maker_fee_cents

        try:
            with get_session() as s:
                row = MMShadowFill(
                    ticker=fill.ticker[:100],
                    side=fill.side,
                    price_cents=fill.price_cents,
                    count=fill.count,
                    fee_cents=kalshi_fee_cents(fill.count, fill.price_cents),
                    fee_model="maker" if self._fees_as_maker else "taker",
                    fee_effective_cents=(
                        kalshi_maker_fee_cents(fill.count, fill.price_cents)
                        if self._fees_as_maker
                        else kalshi_fee_cents(fill.count, fill.price_cents)
                    ),
                    rule=fill.rule[:50],
                    inventory_after=inventory_after,
                    quote_fair_prob=round(quote_fair_prob, 4)
                    if quote_fair_prob is not None
                    else None,
                    fill_fair_prob=round(fill_fair_prob, 4) if fill_fair_prob is not None else None,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    mark_jump_cents=round(mark_jump, 2) if mark_jump is not None else None,
                    quote_jump_cents=round(quote_jump, 2) if quote_jump is not None else None,
                )
                s.add(row)
                s.commit()
                s.refresh(row)
                return row.id
        except Exception:
            logger.exception("motor5.persist_fill_error")
        return None

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
