"""
RestExecutor — ejecutor nativo del Motor REST, AISLADO del Motor 1.

Implementa el diseño aprobado en docs/motor_rest_design.md §4.2/4.3:
  - FOK nativo (time_in_force="fill_or_kill"). Fase 3b: la pata HARD (cara/fina, la
    que empíricamente KILL-ea) se dispara PRIMERO y se espera; solo si llena se
    disparan las baratas. Si la hard KILL-ea, no se compra nada → no-op (cero pérdida).
  - Manejo EXPLÍCITO por pata → máquina de 4 estados: FILL / KILL / ERROR_RED.
    NO se usa asyncio.gather(return_exceptions=True) (prohibido, Lección 7) ni se
    asume "excepción de red = no ejecutó" (eso es el bug de Issue #14): un
    ERROR_RED es estado DESCONOCIDO → se reconcilia con get_positions/get_orders
    antes de decidir rollback.
  - Rollback robusto: Kalshi NO tiene órdenes market → el rollback es limit
    agresivo (sell-to-1¢) que PUEDE no llenarse → reintento acotado → kill-switch
    (pausa + alerta CRITICAL), nunca "queda expuesto a la espera de un humano".
  - Circuit breaker: 3 rollbacks en 1h → pausa, rechaza nuevas órdenes, log CRITICAL.

NO importa nada del Motor 1 (executor.py de motor_1_arbitrage queda intacto y
aislado). Solo corre con TRADING_ENABLED=True; el caller es responsable del muro.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from loguru import logger
from sqlmodel import select

from src.clients.kalshi_rest import KalshiClientError, KalshiRestClient
from src.math.arbitrage import ArbLeg, ArbOpportunity, select_hard_leg
from src.math.fees import kalshi_fee_cents
from src.storage.models import Trade, engage_kill_switch, get_session


def _naive_utc_now() -> datetime:
    """Convención del proyecto para writes a Trade (ver manager.py): naive UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


# error.code que Kalshi devuelve (HTTP 409) cuando una orden fill_or_kill NO encuentra
# volumen para llenarse → es un KILL DETERMINÍSTICO (la orden llegó y se rechazó limpio),
# NO un ERROR_RED. Verificado contra la API viva (demo, 2026-06-05).
# [verificar] ¿Es el ÚNICO code que significa "FOK no llenó"? Con este se cubre el caso
# probado; si en prod aparece otro 409 relacionado a FOK, se suma acá. Fallo conservador:
# cualquier code distinto → se repropaga → ERROR_RED → reconcilia.
_FOK_KILL_ERROR_CODES = frozenset({"fill_or_kill_insufficient_resting_volume"})


def _as_int(value: object) -> int | None:
    """Castea a int un campo que puede venir int o fixed-point string. None si inválido."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(round(float(value)))
        except (ValueError, TypeError):
            return None
    return None


class LegState(StrEnum):
    """Estado resuelto de una pata tras intentar ejecutarla."""

    FILL = "FILL"  # ejecutada completa (FOK confirmó fill)
    KILL = "KILL"  # NO ejecutada, cancelada limpia (FOK no llenó)
    ERROR_RED = "ERROR_RED"  # estado DESCONOCIDO: excepción de red/timeout — pudo llenar o no


@dataclass(frozen=True, slots=True)
class LegResult:
    leg: ArbLeg
    state: LegState
    client_order_id: str
    order_id: str | None = None  # id de Kalshi si se conoce


@dataclass
class ExecutionOutcome:
    """Resultado de ejecutar una oportunidad."""

    filled: bool  # ambas patas FILL
    leg_states: list[LegState] = field(default_factory=list)
    rollback_triggered: bool = False
    rollback_filled: bool = False
    reconciled: bool = False
    kill_switch_fired: bool = False
    rejected_paused: bool = False  # rechazada por circuit breaker activo


class RestExecutor:
    """
    Ejecutor FOK del Motor REST. Una instancia por motor; mantiene el estado del
    circuit breaker en memoria.

    NO ES RE-ENTRANTE: `execute()` no debe llamarse concurrentemente sobre la misma
    instancia. `_paused` y `_rollback_timestamps` no tienen lock; en asyncio
    single-thread con llamadas SECUENCIALES (el uso previsto) es seguro, pero
    invocaciones solapadas tendrían carrera en el circuit breaker. El caller debe
    serializar las ejecuciones. [validar/ajustar en demo si el motor paraleliza]
    """

    ROLLBACK_WINDOW_SEC = 3600.0  # ventana del circuit breaker
    CIRCUIT_BREAKER_THRESHOLD = 3  # rollbacks en la ventana antes de pausar
    ROLLBACK_MAX_RETRIES = 3  # reintentos del rollback limit antes de kill-switch
    ROLLBACK_PRICE_CENTS = 1  # sell agresivo a 1¢ para consumir cualquier bid

    def __init__(self, client: KalshiRestClient) -> None:
        self.client = client
        self._rollback_timestamps: list[float] = []  # monotonic, dentro de la ventana
        self._paused = False

    # =====================================================
    # API pública
    # =====================================================

    async def execute(self, opp: ArbOpportunity) -> ExecutionOutcome:
        """
        Ejecuta una oportunidad de arbitraje con FOK, SECUENCIANDO la pata HARD primero.

        Fase 3b — guardrail "hard-leg-first": en vez de disparar TODAS las patas en
        paralelo (bug confirmado en prod: la pata cara/favorita KILL-ea por falta de
        volumen resting mientras las patas baratas profundas LLENAN → quedan patas
        huérfanas que el rollback liquida a 1¢ con PÉRDIDA GARANTIZADA cada ciclo),
        se dispara PRIMERO la pata más difícil (la cara/fina, la que empíricamente
        KILL-ea) y se ESPERA su resultado. Solo si LLENA se disparan las patas baratas
        restantes. Si la hard KILL-ea, no se compró NADA → cero exposición → no-op (en
        vez de un rollback con pérdida). Esto convierte ~91% de las pérdidas en no-ops.

        Returns ExecutionOutcome con el desenlace. NUNCA deja exposición silenciosa:
        toda pata huérfana se rollbackea o escala a kill-switch.
        """
        if self._paused:
            logger.critical("rest_exec.rejected reason=circuit_breaker_paused")
            return ExecutionOutcome(filled=False, rejected_paused=True)

        # A.1 — arb_id compartido por todas las patas, en notes (fuente PRIMARIA de
        # agrupación de PR-B/settlement vía 'arb_id='). El coid lleva un ÍNDICE además del
        # side para ser ÚNICO por pata: en el arb multi-outcome (1X2) TODAS las patas son
        # side="yes", así que sin el índice colisionarían y el unique de client_order_id
        # haría fallar _persist_intents. El binario (yes+no) también queda único.
        # [verificar en smoke] Kalshi acepta el coid (uuid36 + '-{i}-{side}' ≤ 100 chars).
        arb_id = str(uuid.uuid4())
        coids = {leg: f"{arb_id}-{i}-{leg.side}" for i, leg in enumerate(opp.legs)}

        # A.1 — INTENTS PRE-RED: la fila Trade por pata se escribe ANTES de tocar la
        # red. Es lo que permite que reconcile_pending_trades (boot) recupere un crash
        # a mitad de ejecución, y lo que alimenta exposición/stop-loss del RiskManager.
        # Si el persist falla → ABORT sin colocar órdenes (sin fila, el RiskManager
        # quedaría ciego a un fill real — fail-safe hacia no-operar). Se persisten TODAS
        # las patas (incluidas las baratas que quizá no se envíen): el RiskManager
        # necesita los intents y el reconcile de boot las cierra si quedan pending.
        if not self._persist_intents(arb_id, opp, coids):
            return ExecutionOutcome(filled=False)

        # HARD LEG = la pata más cara (mayor price_cents; desempate por menor
        # available_size). Empíricamente (prod, 4/4 KILLs) es la favorita cara/fina la que
        # NO encuentra volumen resting y KILL-ea. Disparar la FOK secuenciada sobre ella ES
        # el test barato de profundidad viva: un KILL ahora no cuesta nada (no se compró
        # nada), así que NO hace falta un pre-check de depth. select_hard_leg es la FUENTE
        # ÚNICA que comparten esta ejecución y el log de intención shadow del engine.
        hard_leg, other_legs = select_hard_leg(opp)

        # Paso 1: disparar SOLO la pata hard y esperarla.
        hard_result = await self._place_fok(hard_leg, coids[hard_leg])
        self._apply_leg_result_to_trade(hard_result)

        # Caso KILL de la hard: no se compró nada → no-op, cero exposición. Las patas
        # baratas NO se envían (su fila pending se cierra como cancelada).
        if hard_result.state is LegState.KILL:
            self._cancel_unsent_legs(other_legs, coids)
            logger.info(
                f"rest_exec.hard_leg_kill ticker={hard_leg.market_ticker} "
                f"side={hard_leg.side} price={hard_leg.price_cents}c → no se envían "
                f"{len(other_legs)} patas baratas (no-op, cero exposición)"
            )
            # leg_states sobre el orden de opp.legs: la hard con su KILL, las demás KILL
            # (terminaron no-llenas). El orden es solo auditoría; se mantiene estable.
            leg_states = [hard_result.state if lg is hard_leg else LegState.KILL for lg in opp.legs]
            return ExecutionOutcome(filled=False, leg_states=leg_states, rollback_triggered=False)

        # Caso ERROR_RED de la hard: estado DESCONOCIDO. NO perseguimos las patas
        # baratas durante red degradada; reconciliamos la hard sola y, si resultó llena,
        # rollback de esa única pata expuesta.
        if hard_result.state is LegState.ERROR_RED:
            self._cancel_unsent_legs(other_legs, coids)
            outcome = ExecutionOutcome(filled=False, leg_states=[hard_result.state])
            filled_legs = await self._resolve_exposure([hard_result], outcome)
            if filled_legs:
                await self._rollback(filled_legs, outcome)
            return outcome

        # Paso 2: la hard LLENÓ → ahora sí disparar las patas baratas (profundas, pueden
        # ir en paralelo ENTRE ELLAS) con manejo EXPLÍCITO por pata.
        tasks = [asyncio.create_task(self._place_fok(lg, coids[lg])) for lg in other_legs]
        cheap_results: list[LegResult] = [await t for t in tasks]
        for r in cheap_results:
            self._apply_leg_result_to_trade(r)

        results = [hard_result, *cheap_results]
        states = [r.state for r in results]
        outcome = ExecutionOutcome(filled=False, leg_states=states)

        # Caso 1: todas FILL → arb capturado completo.
        if all(s is LegState.FILL for s in states):
            outcome.filled = True
            logger.info(f"rest_exec.filled legs={len(results)} net={opp.net_profit_cents}c")
            return outcome

        # Caso residual (asimétrico/ERROR_RED tras la hard llena, raro) → reconciliar y,
        # si quedó exposición, rollback de las patas efectivamente llenas.
        filled_legs = await self._resolve_exposure(results, outcome)
        if filled_legs:
            await self._rollback(filled_legs, outcome)
        return outcome

    def _apply_leg_result_to_trade(self, r: LegResult) -> None:
        """
        Actualiza la fila Trade de una pata a su estado resuelto.

        FILL es CRÍTICO (posición real abierta: si la DB falla acá, el RiskManager queda
        ciego → pausa preventiva). KILL es best-effort (sin posición; la fila pending
        residual la marca el reconcile de boot con alerta conservadora). ERROR_RED no se
        toca acá: lo resuelve _resolve_exposure tras reconciliar.
        """
        if r.state is LegState.FILL:
            self._update_trade(
                r.client_order_id,
                critical=True,
                status="filled",
                filled_at=_naive_utc_now(),
                fill_price_cents=r.leg.price_cents,
                fees_cents=kalshi_fee_cents(r.leg.count, r.leg.price_cents),
                kalshi_order_id=r.order_id,
            )
        elif r.state is LegState.KILL:
            self._update_trade(
                r.client_order_id,
                critical=False,
                status="cancelled",
                notes_append="fok_kill",
            )

    def _cancel_unsent_legs(self, legs: list[ArbLeg], coids: dict[ArbLeg, str]) -> None:
        """
        Cierra como canceladas las filas pending de patas que NUNCA se enviaron a la red
        (porque la hard KILL-eó o quedó ERROR_RED). Best-effort: sin posición real, solo
        marca de auditoría para distinguirlas de un KILL real (no_sent ≠ fok_kill).
        """
        for leg in legs:
            self._update_trade(
                coids[leg],
                critical=False,
                status="cancelled",
                notes_append="not_sent_hard_first",
            )

    # =====================================================
    # Ejecución de pata (FOK)
    # =====================================================

    async def _place_fok(self, leg: ArbLeg, coid: str) -> LegResult:
        """
        Coloca una pata con FOK. Resuelve a FILL / KILL / ERROR_RED.

        - FILL  : HTTP 200, fill_count completo y remaining 0.
        - KILL  : HTTP 409 con error.code de "FOK sin volumen" → rechazo determinístico.
        - ERROR_RED: excepción de red/timeout o cualquier otro error → DESCONOCIDO.
        """
        try:
            resp = await self.client.place_order(
                ticker=leg.market_ticker,
                side=leg.side,
                action="buy",
                count=leg.count,
                order_type="limit",
                yes_price=leg.price_cents if leg.side == "yes" else None,
                no_price=leg.price_cents if leg.side == "no" else None,
                client_order_id=coid,
                time_in_force="fill_or_kill",
            )
        except KalshiClientError as exc:
            # Kalshi modela el KILL de un FOK como HTTP 409 + error.code específico, NO como
            # un order object con status canceled. Es un rechazo DETERMINÍSTICO: la orden
            # llegó y no había volumen → KILL limpio, nada que reconciliar.
            # Match ESTRICTO: solo 409 + code conocido es KILL. Cualquier otro 409 (orden
            # malformada, mercado cerrado, etc.) o cualquier otro code → ERROR_RED (se
            # reconcilia). Fallo conservador hacia "estado desconocido".
            if exc.status_code == 409 and exc.error_code in _FOK_KILL_ERROR_CODES:
                logger.info(
                    f"rest_exec.leg.kill ticker={leg.market_ticker} side={leg.side} "
                    f"code={exc.error_code} (FOK sin volumen, KILL determinístico)"
                )
                return LegResult(leg=leg, state=LegState.KILL, client_order_id=coid)
            # Otro error de cliente (4xx) → desconocido, no asumir KILL.
            logger.warning(
                f"rest_exec.leg.error_red ticker={leg.market_ticker} side={leg.side} "
                f"status={exc.status_code} code={exc.error_code}: {exc}"
            )
            return LegResult(leg=leg, state=LegState.ERROR_RED, client_order_id=coid)
        except Exception as exc:
            # Excepción de red/timeout: la orden pudo llegar a Kalshi y llenarse → DESCONOCIDO.
            logger.warning(
                f"rest_exec.leg.error_red ticker={leg.market_ticker} side={leg.side}: {exc}"
            )
            return LegResult(leg=leg, state=LegState.ERROR_RED, client_order_id=coid)

        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        order_id = str(order.get("order_id", "")) or None
        if self._create_order_filled(order, leg):
            return LegResult(leg=leg, state=LegState.FILL, client_order_id=coid, order_id=order_id)
        # FOK que no llenó (remaining_count > 0 / fill_count < count) → cancelado limpio.
        return LegResult(leg=leg, state=LegState.KILL, client_order_id=coid, order_id=order_id)

    @staticmethod
    def _create_order_filled(order: dict, leg: ArbLeg) -> bool:
        """
        True si la respuesta de CreateOrder indica fill COMPLETO de la pata.

        Shape real de Kalshi CreateOrder (V2), verificado contra la doc:
        https://docs.kalshi.com/api-reference/orders/create-order
          - fill_count: contratos llenados inmediatamente al colocar.
          - remaining_count: contratos restantes; para FOK/IOC es el estado final
            tras cancelar lo no llenado.
        NO existe un campo `status` en CreateOrder; tampoco `filled`/`count_filled`.

        FALLO CONSERVADOR HACIA KILL: si los campos esperados no están presentes o
        no son enteros, se devuelve False (KILL). Mejor un rollback innecesario que
        leer un KILL como FILL → exposición direccional silenciosa (Issue #14).

        ⚠️ [verificar en demo] El nombre exacto puede venir como fixed-point
        (`fill_count_fp`/`remaining_count_fp`, strings) según endpoint/versión. Se
        prueban ambos; lo que no se confirme aquí se valida mandando una FOK real
        en cuenta demo y observando la respuesta cruda antes de operar capital.
        """
        fill_count = _as_int(order.get("fill_count", order.get("fill_count_fp")))
        remaining = _as_int(order.get("remaining_count", order.get("remaining_count_fp")))
        if fill_count is None:
            return False  # sin señal de fill confiable → KILL conservador
        # Fill completo: se llenó al menos lo pedido y no quedó nada pendiente.
        if remaining is not None:
            return fill_count >= leg.count and remaining == 0
        return fill_count >= leg.count

    @staticmethod
    def _get_orders_filled(order: dict, leg: ArbLeg) -> bool:
        """
        True si una orden de GetOrders/GetOrder está ejecutada (para reconciliación).

        Shape real de Kalshi GetOrders, verificado:
        https://docs.kalshi.com/api-reference/orders/get-orders
          - status ∈ {"resting", "canceled", "executed"}.
        FILL = status == "executed". Fallo conservador: cualquier otro → no llena.
        """
        return str(order.get("status", "")).lower() == "executed"

    # =====================================================
    # Reconciliación de ERROR_RED (estado desconocido)
    # =====================================================

    async def _resolve_exposure(
        self, results: list[LegResult], outcome: ExecutionOutcome
    ) -> list[LegResult]:
        """
        Determina qué patas quedaron REALMENTE llenas (exposición a cerrar).

        FILL → llena. KILL → no llena. ERROR_RED → desconocido: consultar posición
        real (get_positions / get_orders por client_order_id) antes de decidir.
        Devuelve la lista de patas confirmadas llenas que requieren rollback.
        """
        exposed: list[LegResult] = []
        for r in results:
            if r.state is LegState.FILL:
                exposed.append(r)
            elif r.state is LegState.ERROR_RED:
                outcome.reconciled = True
                really_filled = await self._reconcile_leg(r)
                if really_filled:
                    logger.warning(
                        f"rest_exec.reconcile ticker={r.leg.market_ticker} → FILLED (rollback)"
                    )
                    # A.1 — la pata desconocida resultó LLENA: posición real → crítico.
                    self._update_trade(
                        r.client_order_id,
                        critical=True,
                        status="filled",
                        filled_at=_naive_utc_now(),
                        fill_price_cents=r.leg.price_cents,
                        fees_cents=kalshi_fee_cents(r.leg.count, r.leg.price_cents),
                        notes_append="error_red_reconciled_filled",
                    )
                    exposed.append(r)
                else:
                    logger.info(f"rest_exec.reconcile ticker={r.leg.market_ticker} → not filled")
                    # A.1 — confirmada no-llena por doble fuente → cerrar la fila.
                    self._update_trade(
                        r.client_order_id,
                        critical=False,
                        status="cancelled",
                        notes_append="error_red_reconciled",
                    )
        return exposed

    async def _reconcile_leg(self, r: LegResult) -> bool:
        """
        Consulta el estado REAL de una pata ERROR_RED con DOS fuentes independientes.

        Es la decisión más cara del sistema, así que no descansa en una sola fuente
        (que además comparte el shape de parsing dudoso):
          1) PRIMARIA: get_orders, match por client_order_id → status=="executed".
          2) SECUNDARIA: get_positions, ¿hay posición abierta en el ticker?

        Reglas (fail-safe hacia exposición = rollback, la opción segura):
          - Si CUALQUIERA de las dos consultas falla (red sigue caída) → exposición.
          - Si DISCREPAN (una dice llena, la otra no) → exposición (no confiar en
            la fuente optimista).
          - Solo si AMBAS coinciden en "no llena" → no llena (no rollback).
        """
        # Fuente 1: get_orders por client_order_id.
        try:
            resp = await self.client.get_orders(ticker=r.leg.market_ticker)
            orders = resp.get("orders", []) if isinstance(resp, dict) else []
            matched = next(
                (o for o in orders if str(o.get("client_order_id", "")) == r.client_order_id),
                None,
            )
            # No apareció la orden → probablemente no llegó a Kalshi (no llena por esta fuente).
            orders_says_filled = self._get_orders_filled(matched, r.leg) if matched else False
        except Exception as exc:
            logger.critical(
                f"rest_exec.reconcile.get_orders_failed ticker={r.leg.market_ticker}: {exc} → assume exposed"
            )
            return True

        # Fuente 2: get_positions, independiente del parsing de órdenes.
        try:
            has_position = await self._has_open_position(r.leg.market_ticker)
        except Exception as exc:
            logger.critical(
                f"rest_exec.reconcile.get_positions_failed ticker={r.leg.market_ticker}: {exc} → assume exposed"
            )
            return True

        if orders_says_filled == has_position:
            return orders_says_filled  # ambas coinciden
        # Discrepancia entre fuentes → tratar como exposición (rollback, lo seguro).
        logger.critical(
            f"rest_exec.reconcile.discrepancy ticker={r.leg.market_ticker} "
            f"get_orders={orders_says_filled} get_positions={has_position} → assume exposed"
        )
        return True

    async def _has_open_position(self, ticker: str) -> bool:
        """True si hay una posición abierta (no-cero) en el ticker, vía get_positions."""
        resp = await self.client.get_positions()
        positions = (
            resp.get("market_positions", resp.get("positions", []))
            if isinstance(resp, dict)
            else []
        )
        for p in positions:
            if str(p.get("ticker", "")) == ticker:
                # Contratos netos != 0 → posición abierta. Kalshi devuelve el numérico como
                # fixed-point string en `position_fp` (ej. "-1.00"); el `position` plano puede
                # no venir. Leer _fp primero con fallback al plano (robusto ante ambos shapes).
                pos = _as_int(p.get("position_fp", p.get("position")))
                if pos is not None and pos != 0:
                    return True
        return False

    # =====================================================
    # Rollback + circuit breaker
    # =====================================================

    async def _rollback(self, filled_legs: list[LegResult], outcome: ExecutionOutcome) -> None:
        """
        Liquida las patas expuestas con sell agresivo a 1¢ (Kalshi no tiene market).

        El rollback limit PUEDE no llenarse → reintento acotado → si agota, kill-switch.
        Registra el evento en el circuit breaker.
        """
        outcome.rollback_triggered = True
        self._record_rollback()  # cuenta para el circuit breaker

        all_closed = True
        for r in filled_legs:
            closed = await self._sell_to_exit(r.leg)
            all_closed = all_closed and closed
            if closed:
                # A.1 — la pérdida del rollback se REALIZA al instante: settled +
                # pnl_cents NEGATIVO a PRECIOS LÍMITE (venta a ROLLBACK_PRICE_CENTS=1¢).
                # SESGO CONSERVADOR documentado: el sell IOC pudo llenar a mejor precio
                # (al bid); registrar 1¢ sobrestima la pérdida → seguro para el stop-loss.
                # [verificar en smoke] leer el precio real vía get_fills y reemplazar.
                pnl = (
                    (self.ROLLBACK_PRICE_CENTS - r.leg.price_cents) * r.leg.count
                    - kalshi_fee_cents(r.leg.count, r.leg.price_cents)
                    - kalshi_fee_cents(r.leg.count, self.ROLLBACK_PRICE_CENTS)
                )
                self._update_trade(
                    r.client_order_id,
                    critical=True,
                    status="settled",
                    settled_at=_naive_utc_now(),
                    pnl_cents=pnl,
                    notes_append="rollback_closed",
                )
            else:
                # A.1 — la pata queda EXPUESTA (kill-switch a continuación): se mantiene
                # status="filled" para que el cap de exposición del 25% LA VEA, con
                # marca de auditoría para el runbook.
                self._update_trade(
                    r.client_order_id,
                    critical=True,
                    notes_append="exposed_killswitch",
                )

        outcome.rollback_filled = all_closed
        if not all_closed:
            # El rollback no convergió → kill-switch: pausar + alerta CRITICAL.
            outcome.kill_switch_fired = True
            self._paused = True
            await self._fire_kill_switch(filled_legs)
        # Tras registrar el rollback, evaluar si el circuit breaker debe pausar.
        self._maybe_trip_circuit_breaker()

    async def _sell_to_exit(self, leg: ArbLeg) -> bool:
        """Sell a 1¢ con reintentos. True si se llenó (posición cerrada)."""
        for attempt in range(self.ROLLBACK_MAX_RETRIES):
            try:
                resp = await self.client.place_order(
                    ticker=leg.market_ticker,
                    side=leg.side,
                    action="sell",
                    count=leg.count,
                    order_type="limit",
                    yes_price=self.ROLLBACK_PRICE_CENTS if leg.side == "yes" else None,
                    no_price=self.ROLLBACK_PRICE_CENTS if leg.side == "no" else None,
                    client_order_id=str(uuid.uuid4()),
                    time_in_force="immediate_or_cancel",
                )
                order = resp.get("order", resp) if isinstance(resp, dict) else {}
                if self._create_order_filled(order, leg):
                    logger.info(
                        f"rest_exec.rollback.filled ticker={leg.market_ticker} attempt={attempt + 1}"
                    )
                    return True
                logger.warning(
                    f"rest_exec.rollback.not_filled ticker={leg.market_ticker} "
                    f"attempt={attempt + 1}/{self.ROLLBACK_MAX_RETRIES}"
                )
            except Exception as exc:
                logger.warning(
                    f"rest_exec.rollback.error ticker={leg.market_ticker} attempt={attempt + 1}: {exc}"
                )
        return False

    # =====================================================
    # A.1 — Persistencia de Trade (los ojos del RiskManager)
    # =====================================================

    def _persist_intents(self, arb_id: str, opp: ArbOpportunity, coids: dict[ArbLeg, str]) -> bool:
        """
        Escribe una fila Trade por PATA (status='pending') ANTES de tocar la red.

        Patrón Motor 1 (_persist_intents): el intent pre-red es lo que el reconcile de
        boot usa para recuperar crashes a mitad de ejecución, y lo que la query de
        exposición del RiskManager lee (pending/filled). El arb_id viaja en notes y
        como prefijo del coid. False si la DB falla → el caller ABORTA sin operar.
        """
        try:
            with get_session() as s:
                for leg in opp.legs:
                    s.add(
                        Trade(
                            client_order_id=coids[leg],
                            ticker=leg.market_ticker,
                            side=leg.side,
                            action="buy",
                            count=leg.count,
                            price_cents=leg.price_cents,
                            strategy="motor_rest_arb",
                            estimated_edge_pct=opp.edge_pct,
                            status="pending",
                            notes=f"arb_id={arb_id}",
                        )
                    )
                s.commit()
            return True
        except Exception:
            logger.critical(
                f"rest_exec.persist_intents_failed arb_id={arb_id} → ABORT "
                "(no se colocan órdenes: sin fila, el RiskManager quedaría ciego)"
            )
            return False

    def _update_trade(
        self,
        coid: str,
        *,
        critical: bool,
        notes_append: str | None = None,
        **fields: object,
    ) -> None:
        """
        Actualiza la fila Trade de una pata.

        critical=True → la fila representa una POSICIÓN REAL (fill/settled/expuesta):
        si el update falla, el RiskManager queda ciego a capital vivo → PAUSA
        PREVENTIVA (decisión (ii) del diseño A.1): kill-switch persistente + BotState.
        critical=False → best-effort (KILL/cancelled, sin posición): solo se loguea;
        la fila pending residual la levanta el reconcile de boot con alerta conservadora.
        """
        try:
            with get_session() as s:
                t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                if t is None:
                    logger.warning(f"rest_exec.update_trade.missing coid={coid}")
                    return
                for k, v in fields.items():
                    setattr(t, k, v)
                if notes_append:
                    t.notes = f"{t.notes or ''} {notes_append}".strip()[:500]
                s.add(t)
                s.commit()
        except Exception:
            logger.exception(f"rest_exec.update_trade_failed coid={coid} critical={critical}")
            if critical:
                self._engage_preventive_pause(coid)

    def _engage_preventive_pause(self, coid: str) -> None:
        """
        Pausa preventiva (diseño A.1, opción (ii)): un fill REAL no pudo registrarse →
        el RiskManager está ciego a esa posición → detener nuevas operaciones hasta
        revisión humana. Usa el kill-switch persistente (#32): sobrevive restarts y
        la Capa A no reconstruye el executor. Best-effort: nunca lanza.
        """
        msg = f"persist de Trade falló post-fill (coid={coid}) — RiskManager ciego a posición viva"
        logger.critical(f"rest_exec.preventive_pause {msg}")
        self._paused = True
        try:
            from src.monitoring.health import BotState

            BotState.is_paused = True
            BotState.pause_reason = msg
            engage_kill_switch(msg)
        except Exception:
            logger.exception("rest_exec.preventive_pause.persist_failed")

    def _record_rollback(self) -> None:
        now = time.monotonic()
        self._rollback_timestamps.append(now)
        self._rollback_timestamps = [
            t for t in self._rollback_timestamps if now - t < self.ROLLBACK_WINDOW_SEC
        ]

    def _maybe_trip_circuit_breaker(self) -> None:
        if len(self._rollback_timestamps) >= self.CIRCUIT_BREAKER_THRESHOLD and not self._paused:
            self._paused = True
            logger.critical(
                f"rest_exec.circuit_breaker.tripped rollbacks={len(self._rollback_timestamps)} "
                f"window={self.ROLLBACK_WINDOW_SEC:.0f}s action=pause_executor"
            )

    async def _fire_kill_switch(self, filled_legs: list[LegResult]) -> None:
        """
        Pausa del executor ya seteada por el caller (_paused). Acá se eleva a pausa
        GLOBAL y PERSISTENTE: BotState.is_paused (corta check_pre_trade) + DB
        (sobrevive el restart de Coolify — sin esto, un reinicio des-pausaría el bot
        con la pata todavía expuesta). Best-effort: nada de esto debe impedir la alerta.
        """
        tickers = [r.leg.market_ticker for r in filled_legs]
        msg = f"rest_exec.kill_switch posiciones_expuestas={tickers} rollback_no_convergió"
        logger.critical(msg)
        try:
            from src.monitoring.health import BotState
            from src.storage.models import engage_kill_switch

            BotState.is_paused = True
            BotState.pause_reason = msg
            engage_kill_switch(msg)
        except Exception:
            logger.exception("rest_exec.kill_switch.persist_failed")
        try:
            from src.monitoring.telegram_alerts import alert_error

            await alert_error(
                f"Motor REST KILL-SWITCH: rollback no se llenó, posición abierta {tickers}"
            )
        except Exception:
            logger.exception("rest_exec.kill_switch.telegram_failed")

    @property
    def is_paused(self) -> bool:
        return self._paused
