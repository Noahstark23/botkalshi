"""
Motor1Engine (Motor 1) — arbitraje binario YES+NO sobre el orderbook WS (V2).

Ensambla el OrderbookManagerV2 (alimentado por el WS) + detect_binary_arb + el
ArbitrageExecutor en UN bucle supervisado (1s). Lección 7: nunca
gather(return_exceptions); supervisor explícito que registra el fallo del tick y SIGUE.

Gating (dos capas, patrón Motor 2/3/REST):
  - MOTOR_1_ARBITRAGE_ENABLED (runner): si el engine CORRE (detecta + shadow-graba).
  - TRADING_ENABLED + executor presente (CAPA A): si EJECUTA. En shadow el executor es
    None → detecta y graba EdgeWindow pero NUNCA ejecuta órdenes.

EL MURO (defensa en profundidad):
  - Capa A: el runner SOLO construye el ArbitrageExecutor con TRADING_ENABLED=true. En
    shadow el engine recibe executor=None → estructuralmente incapaz de ejecutar.
  - Capa B: guard en _tick (executor None o TRADING_ENABLED false → no ejecuta).
  - Capa C: el propio ArbitrageExecutor/place_order bloquea entradas con el flag en false.

IDENTIDAD DE COMPLEMENTO (crítica): el OrderbookState/V2 mantiene SOLO BIDS por side
(best_ask es casi siempre None). El arb binario necesita ASKS. Se usa la identidad que
ya usa el Motor REST: comprar NO ≈ vender YES, así que:
    yes_ask_sintetico = 100 - best_NO_bid.price_cents   (profundidad = NO bid size)
    no_ask_sintetico  = 100 - best_YES_bid.price_cents   (profundidad = YES bid size)
La oportunidad se computa SOLO con los dos mejores BIDS. Guard 1 <= ask_sintetico <= 99.
La profundidad de cada ask sintético es la del bid OPUESTO (igual que trigger.py:
no_available_size = yes_bid_size), y detect_binary_arb la guarda en cada leg.available_size.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from loguru import logger

from src.math.arbitrage import detect_binary_arb
from src.monitoring.health import BotState
from src.storage.models import EdgeWindow, get_session
from src.utils.config import get_settings

if TYPE_CHECKING:
    from src.math.arbitrage import ArbOpportunity
    from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor
    from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


class Motor1Engine:
    """Bucle de arbitraje binario WS: detecta cruces YES+NO y (ejecuta si trading on)."""

    LOOP_INTERVAL_SEC = 1.0
    HEARTBEAT_EVERY = 60  # ~1 heartbeat/min al intervalo de 1s

    def __init__(
        self,
        manager: OrderbookManagerV2,
        executor: ArbitrageExecutor | None = None,
        *,
        max_count: int | None = None,
    ) -> None:
        self.settings = get_settings()
        self._manager = manager
        # Capa A: en shadow el executor es None (el runner no lo construye) → el engine
        # es estructuralmente incapaz de ejecutar (detecta y graba EdgeWindow, nada más).
        self._executor = executor
        # detect_binary_arb exige max_count >= 1; None usa su default (100).
        self._max_count = max_count if max_count is not None else 100
        self._ticks = 0
        self._signals_seen = 0

        # Single-flight: una sola ejecución a la vez. Si llega un arb nuevo mientras hay
        # una en curso, se DESCARTA (las ventanas son efímeras; un opp encolado estaría
        # stale). El lock serializa execute() → circuit breaker del executor a salvo.
        self._executing = False
        self._exec_lock = asyncio.Lock()
        # Referencia VIVA al task de ejecución: sin esto asyncio puede GC-earlo a mitad de
        # execute() y cortar la ejecución en silencio (pata abierta). Se limpia en finally.
        self._exec_task: asyncio.Task[None] | None = None

        # De-dupe de EdgeWindow: un arb persistente dispararía una fila/tick (flood). Se
        # graba SOLO cuando la oportunidad cambió (magnitude, count) desde la última grabada
        # para ese ticker. ticker → (magnitude_cents, count) de la última fila grabada.
        self._last_recorded: dict[str, tuple[int, int]] = {}
        # Auditoría rentabilidad 2026-07-07: el "arb" binario intra-ticker equivale a un
        # book AUTO-CRUZADO (yes_bid + no_bid > 100) — un estado que el matching engine
        # elimina en ms; casi toda señal de 1 tick es book local stale. Dos defensas:
        # (a) CONFIRMACIÓN: ejecutar solo si el cruce persistió MOTOR_1_CONFIRM_TICKS
        #     ticks consecutivos (el arb real de varios cents no muere en 1s; el fantasma sí);
        # (b) COOLDOWN por ticker tras una ejecución fallida (KILL/rollback): sin esto el
        #     engine re-martillaba el mismo cruce stale cada ~1s hasta el circuit breaker.
        self._streak: dict[str, int] = {}  # ticker → ticks consecutivos con el cruce vivo
        self._cooldown_until: dict[str, float] = {}  # ticker → monotonic hasta el que no ejecutar
        # Guard de confianza del book (2026-08-06): ejecuciones salteadas por incidente
        # reciente del ticker (visible en el heartbeat — un freno que no se ve = bug).
        self._skips_untrusted = 0
        # Desglose por fuente del embargo (2026-08-08): con 495/495 skips el agregado
        # no distingue "frena fantasmas" de "las re-siembras rutinarias re-arman el
        # embargo más rápido de lo que expira". La calibración se decide con esto.
        self._skips_por_fuente: dict[str, int] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        mode = "LIVE" if self._executor is not None else "SHADOW"
        logger.info(
            f"motor1.engine started ({mode}, trading_enabled={self.settings.TRADING_ENABLED})"
        )
        await self._loop(stop_event)

    async def _loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:
                logger.exception("motor1.engine.tick_failed")
                BotState.record_error(f"motor1.engine: {type(exc).__name__}: {exc}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self.LOOP_INTERVAL_SEC)

    def _detect(self, ticker: str) -> ArbOpportunity | None:
        """
        Detecta arb binario YES+NO para un ticker usando SOLO los mejores bids.

        El book mantiene solo bids; se derivan los asks sintéticos por complemento
        (comprar NO == vender YES). La profundidad de cada ask sintético es la del bid
        OPUESTO. Devuelve None si falta cualquier bid o el ask sintético cae fuera de [1, 99].
        """
        yes_top = self._manager.get_top_of_book(ticker, "yes")
        no_top = self._manager.get_top_of_book(ticker, "no")
        if yes_top is None or no_top is None:
            return None
        yes_bid = yes_top.best_bid
        no_bid = no_top.best_bid
        if yes_bid is None or no_bid is None:
            return None

        # Identidad de complemento: comprar NO == vender YES al bid YES, y viceversa.
        yes_ask_synth = 100 - no_bid.price_cents  # comprar YES ~ contra el bid NO
        no_ask_synth = 100 - yes_bid.price_cents  # comprar NO ~ contra el bid YES
        if not (1 <= yes_ask_synth <= 99) or not (1 <= no_ask_synth <= 99):
            return None

        # Profundidad invertida cross-side: el size disponible para comprar YES (ask
        # sintético) es la profundidad del bid NO; el de comprar NO es la del bid YES.
        return detect_binary_arb(
            ticker,
            yes_ask_cents=yes_ask_synth,
            yes_available_size=no_bid.size,
            no_ask_cents=no_ask_synth,
            no_available_size=yes_bid.size,
            max_count=self._max_count,
            # min(detección, ejecución) — auditoría rentabilidad 2026-07-07: detectar solo
            # a MIN_EDGE_PCT (2.0) hacía INALCANZABLE el piso de ejecución (1.5): los edges
            # en [1.5, 2.0) ni se grababan en EdgeWindow → shadow sesgado y piso ficticio.
            min_edge_pct=min(self.settings.MIN_EDGE_PCT, self.settings.MOTOR_1_EXECUTION_EDGE_PCT),
        )

    async def _tick(self) -> None:
        """Un ciclo: barre los tickers trackeados, detecta y (graba/ejecuta) cada arb."""
        self._ticks += 1
        if self._ticks % self.HEARTBEAT_EVERY == 0:
            logger.info(
                f"motor1.engine.heartbeat ticks={self._ticks} "
                f"tracked={len(self._manager.tracked_tickers)} signals={self._signals_seen} "
                f"skips_book_no_confiable={self._skips_untrusted} "
                f"(incidente={self._skips_por_fuente.get('incidente', 0)} "
                f"siembra={self._skips_por_fuente.get('siembra', 0)})"
            )

        for ticker in self._manager.tracked_tickers:
            opp = self._detect(ticker)
            if opp is None:
                self._streak.pop(ticker, None)  # el cruce murió → la confirmación arranca de 0
                continue

            self._signals_seen += 1
            self._streak[ticker] = self._streak.get(ticker, 0) + 1
            # La detección se GRABA SIEMPRE primero (de-dupe interno evita el flood).
            edge_id = self._record_edge_window(ticker, opp)

            # ── Capa B del muro (guard del path de ejecución) ───────────────────────
            # En shadow el executor NO se construyó (Capa A) → None. El flag es la 2da
            # barrera. Cualquiera corta antes de tocar la red.
            if self._executor is None or not self.settings.TRADING_ENABLED:
                continue

            # CONFIANZA DEL BOOK (forense 2026-08-06): un ticker con incidente PROPIO
            # reciente (desync/incoherencia/clamp del empalme) tiene el book en digestión
            # — su "edge" es sospechoso de fantasma del re-baseo. La prueba pericial: en
            # 44 intentos consecutivos M1 completó CERO arbs; en el evento 21:02 el edge
            # de 3.19% y un `book_incoherent cruce=11¢` 0.4s antes eran EL MISMO BOOK
            # (la pata "fácil" no existía: FOK sin volumen en 141ms, 9¢ de vacío al
            # deshacer). La detección y el EdgeWindow ya se grabaron (shadow intacto);
            # acá se corta SOLO la ejecución. El techo anti-fantasma no cubre esto: un
            # cruce fantasma de 2-5¢ cae DENTRO de la banda plausible.
            trust = self.settings.MOTOR_1_BOOK_TRUST_SEC
            if trust > 0:
                info = self._manager.book_trust_info(ticker)
                if info is not None:
                    edad, fuente = info
                    # SPLIT por fuente (2026-08-08): la primera lectura etiquetada dio
                    # 31/31 fuente=siembra, 0 incidente — el embargo lo re-arman las
                    # re-siembras rutinarias, no los incidentes. Una re-siembra limpia
                    # digiere en ~seam_grace, no necesita los 60s del incidente. El
                    # umbral de siembra es SEPARADO y default 0 = sin split (60s para
                    # todo): el valor lo fija el operador con la distribución del slate.
                    seed_trust = self.settings.MOTOR_1_SEED_TRUST_SEC
                    umbral = seed_trust if (fuente == "siembra" and seed_trust > 0) else trust
                    if edad < umbral:
                        self._skips_untrusted += 1
                        self._skips_por_fuente[fuente] = self._skips_por_fuente.get(fuente, 0) + 1
                        logger.info(
                            f"motor1.exec.book_no_confiable ticker={ticker} "
                            f"edad_book={edad:.1f}s < {umbral:.0f}s fuente={fuente} "
                            f"→ NO ejecuta (total_skips={self._skips_untrusted})"
                        )
                        continue

            # Umbral FINO de ejecución (distinto del MIN_EDGE_PCT de detección).
            if opp.edge_pct < self.settings.MOTOR_1_EXECUTION_EDGE_PCT:
                logger.info(
                    f"motor1.exec.below_threshold ticker={ticker} "
                    f"edge={opp.edge_pct:.2f}% < {self.settings.MOTOR_1_EXECUTION_EDGE_PCT}%"
                )
                continue
            # TECHO anti-fantasma: edge demasiado alto = pata sin precio / mercado a medio
            # resolver. Se grabó el EdgeWindow; aquí se corta SOLO la ejecución.
            if opp.edge_pct > self.settings.MOTOR_1_MAX_EDGE_PCT:
                logger.warning(
                    f"motor1.exec.edge_too_high ticker={ticker} "
                    f"edge={opp.edge_pct:.2f}% > {self.settings.MOTOR_1_MAX_EDGE_PCT}% "
                    "(sospecha de fantasma: pata ~0¢ / mercado por resolver) → NO ejecuta"
                )
                continue

            # CONFIRMACIÓN (auditoría rentabilidad 2026-07-07): el cruce tiene que
            # persistir N ticks consecutivos — un book auto-cruzado real de varios cents
            # no muere en 1s; el fantasma por book stale sí.
            if self._streak.get(ticker, 0) < self.settings.MOTOR_1_CONFIRM_TICKS:
                logger.info(
                    f"motor1.exec.awaiting_confirm ticker={ticker} "
                    f"streak={self._streak.get(ticker, 0)}/{self.settings.MOTOR_1_CONFIRM_TICKS}"
                )
                continue
            # COOLDOWN por ticker tras una ejecución fallida: no re-martillar el mismo
            # cruce stale cada ~1s (el circuit breaker era el único freno → 3 pérdidas).
            now_mono = asyncio.get_event_loop().time()
            if now_mono < self._cooldown_until.get(ticker, 0.0):
                logger.info(
                    f"motor1.exec.cooldown ticker={ticker} "
                    f"restan={self._cooldown_until[ticker] - now_mono:.0f}s"
                )
                continue

            # Single-flight con DESCARTE: si ya hay una ejecución en curso, se descarta
            # este arb (no se encola: estaría stale al terminar la anterior).
            if self._executing:
                logger.info(f"motor1.exec.skip_busy ticker={ticker}")
                continue

            # NO se awaitea inline: se lanza un task y se guarda la referencia (evita el GC
            # del task a mitad de execute()).
            self._executing = True
            self._exec_task = asyncio.create_task(self._execute_and_record(opp, edge_id))

    def _record_edge_window(self, ticker: str, opp: ArbOpportunity) -> int | None:
        """
        Graba la ventana de edge en SQLite (sesión SÍNCRONA, patrón del proyecto).

        DE-DUPE: un arb persistente dispararía una fila/tick (a 1s = flood de edge_windows).
        Solo se graba cuando la oportunidad CAMBIÓ ((magnitude, count) distinto del último
        grabado para ese ticker); si es idéntica se omite y se devuelve None.

        BEST-EFFORT: cualquier fallo devuelve None y NUNCA tira el tick (es registro, no
        decisión). Los campos post-trade quedan en sus defaults (shadow).
        """
        key = (opp.net_profit_cents, opp.count)
        if self._last_recorded.get(ticker) == key:
            return None  # arb sin cambios desde la última grabación → no re-grabar (anti-flood)
        try:
            with get_session() as s:
                window = EdgeWindow(
                    market_ticker=ticker[:100],
                    magnitude_cents=opp.net_profit_cents,  # edge NETO post-comisión
                    gross_spread_cents=opp.gross_profit_cents,  # spread BRUTO pre-comisión
                    count=opp.count,
                    fees_cents=opp.fees_cents,
                    edge_pct=opp.edge_pct,
                    kind="binary",
                )
                s.add(window)
                s.commit()
                s.refresh(window)
                self._last_recorded[ticker] = key
                logger.info(
                    f"motor1.edge.detected ticker={ticker} net={opp.net_profit_cents}c "
                    f"gross={opp.gross_profit_cents}c count={opp.count} edge={opp.edge_pct:.2f}%"
                )
                return window.id
        except Exception:
            logger.exception("motor1.edge.persist_error")
            return None

    async def _execute_and_record(self, opp: ArbOpportunity, edge_id: int | None) -> None:
        """
        Task de ejecución (NO bloquea el tick). Delega TODO en ArbitrageExecutor.execute:
        este ya hace check_pre_trade + resize + rollback. El engine NO reimplementa nada
        de eso — solo llama, loguea y (best-effort) actualiza la fila de EdgeWindow.

        El _exec_lock serializa: dos execute() nunca se solapan (circuit breaker del
        executor a salvo). _executing y _exec_task se limpian en el finally pase lo que pase.
        """
        executor = self._executor
        try:
            if executor is None:  # narrowing (Capa B ya cortó si era None)
                return
            async with self._exec_lock:
                ticker = opp.legs[0].market_ticker if opp.legs else "?"
                # REVALIDACIÓN T-0 (incidente 2026-07-30, día 1 del mes: 3 de 3 rollbacks
                # por la SEGUNDA pata FOK rebotando 409 a 33-35ms de la primera). El opp
                # detectado envejece entre el tick y este punto (single-flight, delays del
                # loop): la premisa "count ≤ available_size por construcción" era verdad AL
                # DETECTAR, no al ejecutar. Se re-detecta del book VIVO acá mismo y se
                # ejecuta el arb FRESCO: si el cruce ya no existe (o dejó de ser rentable),
                # skip LIMPIO — cero órdenes, cero rollback, cero breaker. La carrera
                # residual (RTTs del pre-check de balance + pata dura) es irreducible con
                # FOK secuencial; esto elimina la parte acumulada, que es la grande.
                fresh = self._detect(ticker) if ticker != "?" else None
                if fresh is None:
                    logger.info(
                        f"motor1.exec.revalidation_skip ticker={ticker} — el cruce murió "
                        "entre la detección y la ejecución (skip limpio, sin órdenes)"
                    )
                    self._update_edge_window_outcome(edge_id, False)
                    return
                if fresh.net_profit_cents != opp.net_profit_cents or fresh.count != opp.count:
                    logger.info(
                        f"motor1.exec.revalidated ticker={ticker} "
                        f"net {opp.net_profit_cents}c→{fresh.net_profit_cents}c "
                        f"count {opp.count}→{fresh.count} (se ejecuta el book VIVO)"
                    )
                opp = fresh
                filled = await executor.execute(opp)
                logger.info(
                    f"motor1.exec.outcome edge_id={edge_id} ticker={ticker} "
                    f"filled={filled} net_profit={opp.net_profit_cents}c"
                )
                if not filled and ticker != "?":
                    # Ejecución fallida (KILL/rollback/rechazo): cooldown — si el cruce
                    # sigue "vivo" en el book tras fallar, es casi seguro book stale.
                    self._cooldown_until[ticker] = (
                        asyncio.get_event_loop().time() + self.settings.MOTOR_1_TICKER_COOLDOWN_SEC
                    )
                    self._streak.pop(ticker, None)
                self._update_edge_window_outcome(edge_id, filled)
        except Exception:
            logger.exception("motor1.exec.error")
        finally:
            self._executing = False
            self._exec_task = None

    def _update_edge_window_outcome(self, edge_id: int | None, filled: bool) -> None:
        """
        Pobla la fila de EdgeWindow con el resultado (best-effort). Corre DESPUÉS de
        execute() — es registro, no decisión. Si falla (o edge_id es None), la fila queda
        con defaults y el outcome ya está en el log; NUNCA propaga al flujo de ejecución.
        """
        if edge_id is None:
            return
        try:
            with get_session() as session:
                row = session.get(EdgeWindow, edge_id)
                if row is None:
                    logger.warning(f"motor1.edge.update_missing edge_id={edge_id}")
                    return
                row.leg_states = "FILL/FILL" if filled else "PARTIAL/ROLLBACK"
                row.reconciled = filled
                session.add(row)
                session.commit()
        except Exception:
            logger.exception(f"motor1.edge.update_error edge_id={edge_id}")
