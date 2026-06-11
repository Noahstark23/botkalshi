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

from src.math.arbitrage import ArbOpportunity, detect_binary_arb
from src.monitoring.telegram_alerts import alert_trade
from src.storage.models import EdgeWindow, get_session
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

    def __init__(
        self,
        executor: RestExecutor | None = None,
        risk_manager: RiskManager | None = None,
    ) -> None:
        self.settings = get_settings()
        self._signals_seen = 0       # cruces de arbitraje detectados (EdgeWindows grabadas)
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

        # Heartbeat de observabilidad: evidencia VIVA de que el motor procesa.
        # 0 edges es normal en mercado eficiente; el heartbeat distingue "sin cruces"
        # de "motor zombi". Ahora también reporta si el size llega real (size_real_en
        # X/N) + una muestra cruda del último tick (key=raw->parsed) para confirmar en
        # 30s que el parser NO lee None. Loguea cada HEARTBEAT_EVERY tickers a INFO.
        if self._tickers_evaluated % self.HEARTBEAT_EVERY == 0:
            logger.info(
                f"REST Engine Heartbeat: {self._tickers_evaluated} tickers evaluados, "
                f"{self._signals_seen} cruces detectados | "
                f"size_real_en {self._ticks_with_real_size}/{self.HEARTBEAT_EVERY} ticks | "
                f"ultimo_tick: {bid_key}={bid_raw!r}->{bid_size:.2f} "
                f"{ask_key}={ask_raw!r}->{ask_size:.2f}"
            )
            self._ticks_with_real_size = 0  # reset por ventana

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
                    magnitude_cents=signal.net_edge_cents,       # edge NETO post-comisión
                    gross_spread_cents=signal.gross_spread_cents,  # spread BRUTO pre-comisión
                    count=opp.count,            # reconstrucción exacta del gate
                    fees_cents=opp.fees_cents,
                    edge_pct=opp.edge_pct,
                )
                s.add(window)
                s.commit()
                s.refresh(window)
                return window.id  # para el task de ejecución (update del outcome = Capa 3)
        except Exception:
            logger.exception("motor_rest.edge.persist_error")
            return None
