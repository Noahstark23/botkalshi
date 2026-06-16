"""
Coordinador del Motor REST — wiring del trigger + grabación shadow de EdgeWindow.

Responsabilidad: escuchar el canal WS `ticker`, evaluar el trigger, y en shadow
GRABAR la ventana de edge en SQLite. NO ejecuta órdenes.

EL MURO DE TRADING_ENABLED (defensa en profundidad):
    - En shadow (TRADING_ENABLED=False), este motor NO construye ni instancia el
      path de ejecución. El FOKExecutor no existe aún, así que es natural ahora;
      el principio queda documentado: el path de ejecución NO se instancia con
      TRADING_ENABLED=False.
    - Segunda capa (cuando exista el FOKExecutor): cualquier invocación de la API
      de órdenes debe verificar TRADING_ENABLED lo más abajo posible (idealmente
      un guard que envuelva toda ejecución / el propio cliente REST), de modo que
      aunque el orquestador tenga un bug, la orden no salga.
    - Objetivo: que sea estructuralmente IMPOSIBLE que salga una orden en shadow,
      no que dependa de un único `if`.

El muro completo (3 capas, cable Capas 1-3): Capa A = data_capture solo construye el
RestExecutor con TRADING_ENABLED=true + cliente presente; Capa B = guard en on_ticker
(executor None o flag false → return); Capa C = place_order bloquea ENTRADAS con el
flag en false (kalshi_rest.TradingDisabledError).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from loguru import logger

from src.math.arbitrage import ArbOpportunity, detect_binary_arb, detect_multi_outcome_arb
from src.monitoring.telegram_alerts import alert_trade
from src.storage.models import EdgeWindow, get_session
from src.strategies.data_capture import parse_price_to_cents
from src.strategies.motor_rest_arb.trigger import (
    _YES_ASK_SIZE_KEYS,
    _YES_BID_SIZE_KEYS,
    TriggerSignal,
    _parse_size_float,
    evaluate_ticker,
)
from src.utils.config import get_settings

if TYPE_CHECKING:
    from src.risk.manager import RiskManager
    from src.strategies.motor_rest_arb.executor import ExecutionOutcome, RestExecutor


def _raw_size_value(data: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, Any]:
    """
    Diagnóstico: devuelve (key_encontrada, valor_crudo) del primer candidate presente.

    Si ninguna llave existe, devuelve ("<absent>", None). Permite distinguir en el log
    'campo ausente' de 'campo presente pero None' de 'campo con valor real ("8.73")'.
    """
    for k in keys:
        if k in data:
            return k, data[k]
    return "<absent>", None


class RestArbEngine:
    """
    Motor REST en modo shadow: detecta y graba EdgeWindow, sin ejecutar.

    Wiring (cuando MOTOR_REST_ENABLED=True): ws.on("ticker", self.on_ticker).
    """

    # Cada cuántos tickers evaluados se emite un heartbeat INFO (evidencia de vida).
    HEARTBEAT_EVERY = 200

    # ── P3: multi-outcome SHADOW (1X2/winner del Mundial) ──────────────────────
    # SOLO series con estructura "exactamente UN outcome gana" (mutuamente excluyentes
    # y exhaustivos). NO meter acá series de props/totals (KXWCTEAMGOALS,
    # KXWCGAMEGOALS etc.): sus markets NO son excluyentes → comprar YES en todos NO
    # es arb → señal falsa. El matching es por SERIE EXACTA (primer segmento del
    # ticker), no por prefijo de string: "KXWCGAME" NO debe matchear "KXWCGAMEGOALS".
    MULTI_SERIES = frozenset(
        {
            "KXWCGAME",  # partidos 1X2 — verificado 2026-06-13: 3 markets/evento
            # (ej. KXWCGAME-26JUN27JORARG-{JOR,ARG,TIE})
            "KXWCGROUPWIN",  # ganador de grupo (N excluyentes)
            "KXMENWORLDCUP",  # ganador del torneo
            "KXMWORLDCUP",
        }
    )
    # Frescura: todas las patas del evento deben tener quote con esta edad máxima.
    # Una pata stale (precio viejo) genera arb fantasma → grupo incompleto = no evaluar.
    MULTI_MAX_QUOTE_AGE_SEC = 30.0
    # Mínimo de outcomes para evaluar (1X2 = 3; winner de grupo/torneo ≥ 3).
    MULTI_MIN_LEGS = 3

    def __init__(
        self,
        executor: RestExecutor | None = None,
        risk_manager: RiskManager | None = None,
    ) -> None:
        self.settings = get_settings()
        self._signals_seen = 0  # cruces de arbitraje detectados (EdgeWindows grabadas)
        self._tickers_evaluated = 0  # tickers procesados (para el heartbeat)
        # Diagnóstico de profundidad (instrumentación, NO afecta la lógica del trigger):
        # cuántos tickers de la ventana actual tuvieron size REAL (bid>0 Y ask>0).
        # Resuelve la ambigüedad de "0 cruces": mercado eficiente (size_real alto) vs
        # parser lee None → size 0.0 → el filtro de profundidad descarta TODO (size_real=0).
        self._ticks_with_real_size = 0

        # ── Path de ejecución (Capa 2) ──────────────────────────────────────────
        # En SHADOW estos quedan None (Capa A: data_capture no los construye con el
        # flag en false) → el motor es estructuralmente incapaz de ejecutar.
        self._executor = executor
        self._risk_manager = risk_manager
        # Single-flight: una sola ejecución a la vez. Si llega un arb nuevo mientras
        # hay una en curso, se DESCARTA (las ventanas son efímeras; un opp encolado
        # estaría stale). El lock hace imposible solapar execute() → circuit breaker a salvo.
        self._executing = False
        self._exec_lock = asyncio.Lock()
        # Referencia VIVA al task de ejecución: sin esto asyncio puede GC-earlo a mitad
        # de execute() y cortar la ejecución en silencio (pata abierta). Se limpia en
        # el finally del task, junto con _executing.
        self._exec_task: asyncio.Task[None] | None = None

        # ── P3: estado del multi-outcome shadow ────────────────────────────────
        # Cache del último YES-ask por ticker: ticker → (ask_cents, ask_size, monotonic).
        self._quote_cache: dict[str, tuple[int, int, float]] = {}
        # Universo de eventos multi (lo setea data_capture tras cada discovery):
        # event_key → set de tickers hermanos; ticker → event_key para lookup O(1).
        self._event_universe: dict[str, set[str]] = {}
        self._ticker_to_event: dict[str, str] = {}
        self._multi_signals_seen = 0
        # P4: near-miss por ventana de heartbeat — cuán CERCA estuvo cada forma de arb.
        self._best_binary_gap: tuple[int, str] | None = None  # (ask−bid mínimo, ticker)
        self._best_multi_sum: tuple[int, int, str] | None = None  # (Σasks mínima, n_legs, event)

    def update_universe(self, tickers: set[str]) -> None:
        """
        Reconstruye el universo de eventos multi-outcome desde los tickers trackeados.

        Agrupa por event_key (= ticker sin el sufijo de outcome: rsplit('-', 1)[0]),
        filtrado por MULTI_SERIES_PREFIXES (solo estructuras winner-take-all). Lo llama
        data_capture tras el discovery del boot y tras cada re-discovery.
        """
        universe: dict[str, set[str]] = {}
        for t in tickers:
            # Serie = primer segmento del ticker. Igualdad EXACTA, no startswith
            # (evita que KXWCGAMEGOALS —totales, no excluyentes— caiga en KXWCGAME).
            if t.split("-", 1)[0] not in self.MULTI_SERIES:
                continue
            event_key = t.rsplit("-", 1)[0]
            universe.setdefault(event_key, set()).add(t)
        self._event_universe = universe
        self._ticker_to_event = {t: ev for ev, ts in universe.items() for t in ts}
        n_events = sum(1 for ts in universe.values() if len(ts) >= self.MULTI_MIN_LEGS)
        logger.info(
            f"motor_rest.multi.universe eventos={len(universe)} "
            f"evaluables(≥{self.MULTI_MIN_LEGS} patas)={n_events}"
        )

    async def on_ticker(self, raw_msg: dict[str, Any]) -> None:
        """Handler del canal `ticker`: evaluar trigger y, si hay señal, grabar shadow."""
        self._tickers_evaluated += 1

        # Muestreo de size para diagnóstico (instrumentación pura: NO altera detección).
        # Reusa el parser y las keys del trigger (única fuente de verdad). Mide si el
        # size llega real o None — sin esto, "0 cruces" no es interpretable.
        data = raw_msg.get("msg", raw_msg)
        bid_key, bid_raw = _raw_size_value(data, _YES_BID_SIZE_KEYS)
        ask_key, ask_raw = _raw_size_value(data, _YES_ASK_SIZE_KEYS)
        bid_size = _parse_size_float(data, _YES_BID_SIZE_KEYS)
        ask_size = _parse_size_float(data, _YES_ASK_SIZE_KEYS)
        if bid_size > 0 and ask_size > 0:
            self._ticks_with_real_size += 1

        # ── P3/P4: cache de quotes + near-miss + evaluación multi-outcome ──────────
        # Detección + grabado de EdgeWindow SIEMPRE. La EJECUCIÓN del arb multi-outcome
        # (1X2: comprar YES en todos los outcomes = profit LOCKEADO si Σasks<100) está
        # desbloqueada pero gateada igual que el binario: Capa A (executor construido) +
        # TRADING_ENABLED + umbral de edge. En shadow es no-op (executor None).
        ticker = data.get("market_ticker")
        yes_bid_c = parse_price_to_cents(data.get("yes_bid_dollars"))
        yes_ask_c = parse_price_to_cents(data.get("yes_ask_dollars"))
        if (
            ticker
            and yes_bid_c is not None
            and yes_ask_c is not None
            and 1 <= yes_bid_c <= 99
            and 1 <= yes_ask_c <= 99
        ):
            # P4 near-miss binario: gap = ask − bid (un CRUCE sería gap < 0; cuanto
            # más chico el gap, más cerca estuvo el arb binario de existir).
            gap = yes_ask_c - yes_bid_c
            if self._best_binary_gap is None or gap < self._best_binary_gap[0]:
                self._best_binary_gap = (gap, ticker)
        if ticker and yes_ask_c is not None and 1 <= yes_ask_c <= 99 and ask_size > 0:
            self._quote_cache[ticker] = (yes_ask_c, int(ask_size), time.monotonic())
            event_key = self._ticker_to_event.get(ticker)
            if event_key:
                self._evaluate_multi_outcome(event_key)

        # Heartbeat de observabilidad: evidencia VIVA de que el motor procesa.
        # 0 edges es normal en mercado eficiente; el heartbeat distingue "sin cruces"
        # de "motor zombi". Ahora también reporta si el size llega real (size_real_en
        # X/N) + una muestra cruda del último tick (key=raw->parsed) para confirmar en
        # 30s que el parser NO lee None. Loguea cada HEARTBEAT_EVERY tickers a INFO.
        if self._tickers_evaluated % self.HEARTBEAT_EVERY == 0:
            # P4 — near-miss: cuán cerca estuvo cada forma de arb en esta ventana.
            nm_bin = (
                f"{self._best_binary_gap[0]}c@{self._best_binary_gap[1]}"
                if self._best_binary_gap
                else "n/a"
            )
            nm_multi = (
                f"{self._best_multi_sum[0]}c/{self._best_multi_sum[1]}legs@{self._best_multi_sum[2]}"
                if self._best_multi_sum
                else "n/a"
            )
            logger.info(
                f"REST Engine Heartbeat: {self._tickers_evaluated} tickers evaluados, "
                f"{self._signals_seen} cruces detectados | "
                f"size_real_en {self._ticks_with_real_size}/{self.HEARTBEAT_EVERY} ticks | "
                f"ultimo_tick: {bid_key}={bid_raw!r}->{bid_size:.2f} "
                f"{ask_key}={ask_raw!r}->{ask_size:.2f} | "
                f"multi: {self._multi_signals_seen} señales | "
                f"near-miss bin={nm_bin} multi={nm_multi}"
            )
            self._ticks_with_real_size = 0  # reset por ventana
            self._best_binary_gap = None
            self._best_multi_sum = None

        try:
            signal = evaluate_ticker(
                raw_msg,
                min_edge_cents=self.settings.MOTOR_REST_MIN_EDGE_CENTS,
                min_depth=self.settings.MOTOR_REST_MIN_DEPTH,
            )
        except Exception:
            logger.exception("motor_rest.trigger.eval_error")
            return

        if signal is None:
            return

        self._signals_seen += 1
        edge_id = self._record_edge_window(signal)  # la detección se graba SIEMPRE primero

        # ── Capa B del muro (guard en el path de ejecución) ─────────────────────
        # En shadow el executor NO se construyó (Capa A) → self._executor is None.
        # El flag TRADING_ENABLED es la segunda barrera. Cualquiera corta antes de ejecutar.
        if self._executor is None or not self.settings.TRADING_ENABLED:
            return

        # Umbral FINO de ejecución (distinto del trigger grueso): reusa opp.edge_pct
        # (edge neto post-fee como % del capital comprometido). Solo ejecuta si lo supera.
        opp = signal.opportunity
        if opp.edge_pct < self.settings.MOTOR_REST_EXECUTION_EDGE_PCT:
            logger.info(
                f"motor_rest.exec.below_threshold ticker={opp.legs[0].market_ticker} "
                f"edge={opp.edge_pct:.2f}% < {self.settings.MOTOR_REST_EXECUTION_EDGE_PCT}%"
            )
            return
        # TECHO anti-fantasma: un edge demasiado alto NO es un regalo, es casi siempre una
        # pata sin precio / mercado a medio resolver. Se grabó el EdgeWindow; aquí se corta
        # la EJECUCIÓN (la pérdida #1 del canary sería ejecutar sobre uno de estos).
        if opp.edge_pct > self.settings.MOTOR_REST_MAX_EDGE_PCT:
            logger.warning(
                f"motor_rest.exec.edge_too_high ticker={opp.legs[0].market_ticker} "
                f"edge={opp.edge_pct:.2f}% > {self.settings.MOTOR_REST_MAX_EDGE_PCT}% "
                "(sospecha de fantasma: pata ~0¢ / mercado por resolver) → NO ejecuta"
            )
            return

        # Single-flight con DESCARTE: si ya hay una ejecución en curso, se descarta este
        # arb (no se encola: estaría stale al terminar la anterior). Si sigue vivo,
        # re-dispara en el próximo tick.
        if self._executing:
            logger.info(f"motor_rest.exec.skip_busy ticker={opp.legs[0].market_ticker}")
            return

        # NO se awaitea inline: bloquearía el stream de tickers. Se lanza un task y se
        # guarda la referencia (evita el GC del task a mitad de execute()).
        self._executing = True
        self._exec_task = asyncio.create_task(self._execute_and_record(signal, edge_id))

    async def _execute_and_record(self, signal: TriggerSignal, edge_id: int | None) -> None:
        """
        Task de ejecución (NO bloquea on_ticker). Capa 2: check_pre_trade → resize →
        execute → loguea el outcome. Telegram + update de EdgeWindow son Capa 3 (TODO).

        El _exec_lock serializa: dos execute() nunca se solapan (circuit breaker del
        executor a salvo sin tocar executor.py). _executing y _exec_task se limpian en
        el finally pase lo que pase.
        """
        executor = self._executor
        risk_manager = self._risk_manager
        try:
            # Defensa (no debería pasar: Capa A construye ambos juntos, Capa B ya cortó
            # si executor es None). Narrowing explícito, sin assert (que -O eliminaría).
            if executor is None or risk_manager is None:
                return

            async with self._exec_lock:
                opp = signal.opportunity
                # Gatekeeper de riesgo: caps + stop-loss + is_paused (sizing por caps, cero Kelly).
                decision = await risk_manager.check_pre_trade(opp)
                if not decision.approved:
                    # El rechazo queda registrado en este log (EdgeWindow no tiene campo
                    # "rejected"; la fila de detección ya está grabada con sus defaults).
                    logger.info(
                        f"motor_rest.exec.rejected ticker={opp.legs[0].market_ticker} "
                        f"reason={decision.reason} edge_id={edge_id}"
                    )
                    return

                # Resize al count autorizado REUSANDO detect_binary_arb (recomputa
                # profit/fees/edge coherentes). None → no viable a ese count → NO ejecutar.
                opp_sized = self._resize_opportunity(opp, decision.max_allowed_count)
                if opp_sized is None:
                    logger.info(
                        f"motor_rest.exec.not_viable_resized "
                        f"count={decision.max_allowed_count} edge_id={edge_id} "
                        f"(fees consumen el edge al tamaño autorizado)"
                    )
                    return

                t0 = time.monotonic()
                outcome = await executor.execute(opp_sized)
                cycle_latency_ms = int((time.monotonic() - t0) * 1000)
                # Loguear el outcome (verificación en demo, independiente de Telegram/DB).
                logger.info(
                    f"motor_rest.exec.outcome edge_id={edge_id} filled={outcome.filled} "
                    f"leg_states={[s.value for s in outcome.leg_states]} "
                    f"reconciled={outcome.reconciled} rollback_filled={outcome.rollback_filled} "
                    f"kill_switch={outcome.kill_switch_fired} "
                    f"rejected_paused={outcome.rejected_paused} "
                    f"latency_ms={cycle_latency_ms}"
                )
                # Capa 3 — registro y notificación, AMBOS best-effort: el trade ya pasó;
                # un fallo acá jamás debe afectar el resultado ni tirar el task.
                try:
                    await alert_trade(outcome, opp_sized)
                except Exception:
                    logger.exception("motor_rest.exec.alert_error")
                self._update_edge_window_outcome(edge_id, outcome, cycle_latency_ms)
        except Exception:
            logger.exception("motor_rest.exec.error")
        finally:
            self._executing = False
            self._exec_task = None

    def _update_edge_window_outcome(
        self,
        edge_id: int | None,
        outcome: ExecutionOutcome,
        cycle_latency_ms: int,
    ) -> None:
        """
        Pobla la fila de EdgeWindow (grabada en la detección) con el resultado real.

        BEST-EFFORT: corre DESPUÉS de que execute() retornó — es registro, no decisión.
        Si falla (o edge_id es None porque la detección no se grabó), la fila queda con
        defaults y el outcome ya está en el log; NUNCA propaga al flujo de ejecución.
        rest_rtt_ms queda en default (no medible desde el engine; mejora futura:
        sumarlo al ExecutionOutcome del executor).
        """
        if edge_id is None:
            return
        try:
            with get_session() as session:
                row = session.get(EdgeWindow, edge_id)
                if row is None:
                    logger.warning(f"motor_rest.edge.update_missing edge_id={edge_id}")
                    return
                row.leg_states = "/".join(s.value for s in outcome.leg_states)
                row.reconciled = outcome.reconciled
                row.kill_switch_fired = outcome.kill_switch_fired
                row.rollback_filled = outcome.rollback_filled
                row.cycle_latency_ms = cycle_latency_ms
                session.add(row)
                session.commit()
        except Exception:
            logger.exception(f"motor_rest.edge.update_error edge_id={edge_id}")

    def _resize_opportunity(self, opp: ArbOpportunity, max_count: int) -> ArbOpportunity | None:
        """
        Redimensiona el opp al count autorizado por el risk manager REUSANDO
        detect_binary_arb (una sola fuente de verdad): recomputa gross/fees/net/edge
        coherentes — NO se usa dataclasses.replace, que dejaría esos campos stale →
        loguearíamos/decidiríamos con PnL falso.

        Devuelve None si a ese count menor los fees consumen el edge (no viable) → el
        caller NO ejecuta. Solo arb binario YES+NO (lo único que produce el trigger REST).
        """
        if max_count < 1:
            return None
        # Identificar las patas por side (NO por índice del tuple) → orden-independiente.
        yes_leg = next((leg for leg in opp.legs if leg.side == "yes"), None)
        no_leg = next((leg for leg in opp.legs if leg.side == "no"), None)
        if yes_leg is None or no_leg is None:
            return None  # no es binario YES+NO → no resize (no esperado en Motor REST)
        # INVERSIÓN CROSS-SIDE — ya RESUELTA, no se re-aplica acá:
        # la profundidad del NO-ask = profundidad del YES-bid (comprar NO == vender YES
        # al bid). El trigger ya aplicó esa inversión al construir el opp original
        # (trigger.py: no_available_size = yes_bid_size) y detect_binary_arb la guardó en
        # no_leg.available_size. Por eso acá se pasa leg.available_size DIRECTO: cada pata
        # ya carga su profundidad resuelta para SU acción. Pasar yes_leg→yes_*, no_leg→no_*
        # preserva la inversión; cruzarlas (yes_leg→no_*) la rompería.
        return detect_binary_arb(
            yes_leg.market_ticker,
            yes_ask_cents=yes_leg.price_cents,
            yes_available_size=yes_leg.available_size,
            no_ask_cents=no_leg.price_cents,
            no_available_size=no_leg.available_size,
            max_count=max_count,
        )

    # =====================================================
    # P3 — Multi-outcome (detecta y graba SIEMPRE; ejecuta el arb LOCKEADO si live)
    # =====================================================

    def _evaluate_multi_outcome(self, event_key: str) -> None:
        """
        Evalúa el arb multi-outcome del evento (1X2/winner): comprar YES en TODOS los
        outcomes paga 100¢ seguro si Σasks < 100 post-fee → profit LOCKEADO (no direccional).

        SEGURIDAD ANTI-SEÑAL-FALSA: solo se evalúa con el grupo COMPLETO (todos los
        tickers que el discovery conoce del evento) y FRESCO (cada quote con edad ≤
        MULTI_MAX_QUOTE_AGE_SEC). Un grupo parcial (falta un outcome) o stale haría
        parecer arb lo que no lo es.

        Siempre graba EdgeWindow. Si hay executor (Capa A) + TRADING_ENABLED + el edge
        supera el umbral de ejecución, dispara la compra de las N patas (FOK, vía el
        RestExecutor genérico N-leg). En shadow es no-op.
        """
        members = self._event_universe.get(event_key)
        if not members or len(members) < self.MULTI_MIN_LEGS:
            return
        now = time.monotonic()
        legs: list[tuple[str, int, int]] = []
        for t in members:
            q = self._quote_cache.get(t)
            if q is None or (now - q[2]) > self.MULTI_MAX_QUOTE_AGE_SEC:
                return  # grupo incompleto o stale → NO evaluar (señal falsa imposible)
            legs.append((t, q[0], q[1]))

        sum_asks = sum(ask for _, ask, _ in legs)
        # P4 near-miss multi: la suma mínima vista (100 − Σ = el margen bruto del arb).
        if self._best_multi_sum is None or sum_asks < self._best_multi_sum[0]:
            self._best_multi_sum = (sum_asks, len(legs), event_key)
        if sum_asks >= 100:
            return

        try:
            opp = detect_multi_outcome_arb(event_key, legs)
        except ValueError:
            return  # datos fuera de rango → no es señal
        if opp is None:
            return  # el fee se comió el spread

        self._multi_signals_seen += 1
        logger.info(
            f"motor_rest.multi.detected event={event_key} legs={len(legs)} "
            f"sum_asks={sum_asks}c net={opp.net_profit_cents}c edge={opp.edge_pct:.2f}% "
            f"count={opp.count}"
        )
        edge_id = self._record_multi_edge_window(event_key, opp)

        # ── Ejecución (Capa 2) del arb multi-outcome ───────────────────────────────
        # Capa B del muro: en shadow el executor es None (Capa A no lo construyó) y/o
        # TRADING_ENABLED=false → no se ejecuta. Cualquiera corta antes de tocar la red.
        if self._executor is None or not self.settings.TRADING_ENABLED:
            return
        if opp.edge_pct < self.settings.MOTOR_REST_EXECUTION_EDGE_PCT:
            logger.info(
                f"motor_rest.multi.exec.below_threshold event={event_key} "
                f"edge={opp.edge_pct:.2f}% < {self.settings.MOTOR_REST_EXECUTION_EDGE_PCT}%"
            )
            return
        # TECHO anti-fantasma (ver path binario): un edge enorme en un 1X2 = patas que no
        # suman ~100 (equipo eliminado a ~0¢ / mercado por resolver). Graba pero NO ejecuta.
        if opp.edge_pct > self.settings.MOTOR_REST_MAX_EDGE_PCT:
            logger.warning(
                f"motor_rest.multi.exec.edge_too_high event={event_key} "
                f"edge={opp.edge_pct:.2f}% > {self.settings.MOTOR_REST_MAX_EDGE_PCT}% "
                "(sospecha de fantasma: pata ~0¢ / mercado por resolver) → NO ejecuta"
            )
            return
        # Single-flight COMPARTIDO con el binario: una sola ejecución a la vez (el lock
        # protege el circuit breaker del executor). Si hay una en curso, se descarta.
        if self._executing:
            logger.info(f"motor_rest.multi.exec.skip_busy event={event_key}")
            return
        self._executing = True
        self._exec_task = asyncio.create_task(
            self._execute_multi_and_record(event_key, legs, opp, edge_id)
        )

    def _record_multi_edge_window(self, event_key: str, opp: ArbOpportunity) -> int | None:
        """Graba la ventana multi-outcome y devuelve su id (para poblar el outcome). Best-effort."""
        try:
            with get_session() as s:
                window = EdgeWindow(
                    market_ticker=event_key[:100],
                    magnitude_cents=opp.net_profit_cents,
                    gross_spread_cents=opp.gross_profit_cents,
                    count=opp.count,
                    fees_cents=opp.fees_cents,
                    edge_pct=opp.edge_pct,
                    kind="multi_outcome",
                )
                s.add(window)
                s.commit()
                s.refresh(window)
                return window.id
        except Exception:
            logger.exception("motor_rest.multi.persist_error")
            return None

    async def _execute_multi_and_record(
        self,
        event_key: str,
        legs: list[tuple[str, int, int]],
        opp: ArbOpportunity,
        edge_id: int | None,
    ) -> None:
        """
        Task de ejecución del arb multi-outcome (NO bloquea on_ticker). Mismo patrón que
        el binario: check_pre_trade → resize al count autorizado → executor.execute (FOK
        N-leg) → log + update de EdgeWindow. _executing/_exec_task se limpian en finally.
        """
        executor = self._executor
        risk_manager = self._risk_manager
        try:
            if executor is None or risk_manager is None:
                return
            async with self._exec_lock:
                decision = await risk_manager.check_pre_trade(opp)
                if not decision.approved:
                    logger.info(
                        f"motor_rest.multi.exec.rejected event={event_key} "
                        f"reason={decision.reason} edge_id={edge_id}"
                    )
                    return
                opp_sized = self._resize_multi(event_key, legs, decision.max_allowed_count)
                if opp_sized is None:
                    logger.info(
                        f"motor_rest.multi.exec.not_viable_resized "
                        f"count={decision.max_allowed_count} edge_id={edge_id} "
                        "(fees consumen el edge al tamaño autorizado)"
                    )
                    return

                t0 = time.monotonic()
                outcome = await executor.execute(opp_sized)
                cycle_latency_ms = int((time.monotonic() - t0) * 1000)
                logger.info(
                    f"motor_rest.multi.exec.outcome event={event_key} edge_id={edge_id} "
                    f"filled={outcome.filled} leg_states={[s.value for s in outcome.leg_states]} "
                    f"reconciled={outcome.reconciled} rollback_filled={outcome.rollback_filled} "
                    f"kill_switch={outcome.kill_switch_fired} latency_ms={cycle_latency_ms}"
                )
                try:
                    await alert_trade(outcome, opp_sized)
                except Exception:
                    logger.exception("motor_rest.multi.exec.alert_error")
                self._update_edge_window_outcome(edge_id, outcome, cycle_latency_ms)
        except Exception:
            logger.exception("motor_rest.multi.exec.error")
        finally:
            self._executing = False
            self._exec_task = None

    def _resize_multi(
        self, event_key: str, legs: list[tuple[str, int, int]], max_count: int
    ) -> ArbOpportunity | None:
        """
        Redimensiona el arb multi-outcome al count autorizado REUSANDO
        detect_multi_outcome_arb (recomputa gross/fees/net/edge coherentes). None si a
        ese count los fees consumen el edge (no viable) → el caller NO ejecuta.
        """
        if max_count < 1:
            return None
        try:
            return detect_multi_outcome_arb(event_key, legs, max_count=max_count)
        except ValueError:
            return None

    def _record_edge_window(self, signal: TriggerSignal) -> int | None:
        """
        Graba la ventana de edge en SQLite (sesión SÍNCRONA, patrón del proyecto).

        Shadow: solo observación. Los campos post-trade (leg_states, reconciled,
        kill_switch_fired, rollback_filled) quedan en sus defaults.
        """
        logger.info(
            f"motor_rest.edge.detected ticker={signal.market_ticker} "
            f"net_edge={signal.net_edge_cents}c gross={signal.gross_spread_cents}c "
            f"depth={signal.limiting_depth}"
        )
        try:
            with get_session() as s:
                opp = signal.opportunity
                window = EdgeWindow(
                    market_ticker=signal.market_ticker,
                    magnitude_cents=signal.net_edge_cents,  # edge NETO post-comisión
                    gross_spread_cents=signal.gross_spread_cents,  # spread BRUTO pre-comisión
                    count=opp.count,  # reconstrucción exacta del gate
                    fees_cents=opp.fees_cents,
                    edge_pct=opp.edge_pct,
                    kind="binary",
                )
                s.add(window)
                s.commit()
                s.refresh(window)
                return window.id  # para el task de ejecución (update del outcome = Capa 3)
        except Exception:
            logger.exception("motor_rest.edge.persist_error")
            return None
