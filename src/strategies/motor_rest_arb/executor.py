"""
RestExecutor — ejecutor nativo del Motor REST, AISLADO del Motor 1.

Implementa el diseño aprobado en docs/motor_rest_design.md §4.2/4.3:
  - FOK nativo (time_in_force="fill_or_kill") en ambas patas, en paralelo.
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
from enum import StrEnum

from loguru import logger

from src.clients.kalshi_rest import KalshiRestClient
from src.math.arbitrage import ArbLeg, ArbOpportunity


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

    FILL = "FILL"            # ejecutada completa (FOK confirmó fill)
    KILL = "KILL"            # NO ejecutada, cancelada limpia (FOK no llenó)
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

    filled: bool                       # ambas patas FILL
    leg_states: list[LegState] = field(default_factory=list)
    rollback_triggered: bool = False
    rollback_filled: bool = False
    reconciled: bool = False
    kill_switch_fired: bool = False
    rejected_paused: bool = False      # rechazada por circuit breaker activo


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

    ROLLBACK_WINDOW_SEC = 3600.0       # ventana del circuit breaker
    CIRCUIT_BREAKER_THRESHOLD = 3      # rollbacks en la ventana antes de pausar
    ROLLBACK_MAX_RETRIES = 3           # reintentos del rollback limit antes de kill-switch
    ROLLBACK_PRICE_CENTS = 1           # sell agresivo a 1¢ para consumir cualquier bid

    def __init__(self, client: KalshiRestClient) -> None:
        self.client = client
        self._rollback_timestamps: list[float] = []  # monotonic, dentro de la ventana
        self._paused = False

    # =====================================================
    # API pública
    # =====================================================

    async def execute(self, opp: ArbOpportunity) -> ExecutionOutcome:
        """
        Ejecuta una oportunidad de arbitraje binario con FOK en ambas patas.

        Returns ExecutionOutcome con el desenlace. NUNCA deja exposición silenciosa:
        toda pata huérfana se rollbackea o escala a kill-switch.
        """
        if self._paused:
            logger.critical("rest_exec.rejected reason=circuit_breaker_paused")
            return ExecutionOutcome(filled=False, rejected_paused=True)

        coids = {leg: str(uuid.uuid4()) for leg in opp.legs}

        # Lanzar ambas patas en paralelo con manejo EXPLÍCITO por pata (no gather
        # que colapsa estados). Cada tarea resuelve a LegResult con su estado.
        tasks = [asyncio.create_task(self._place_fok(leg, coids[leg])) for leg in opp.legs]
        results: list[LegResult] = [await t for t in tasks]

        states = [r.state for r in results]
        outcome = ExecutionOutcome(filled=False, leg_states=states)

        # Caso 1: ambas FILL → arb capturado completo.
        if all(s is LegState.FILL for s in states):
            outcome.filled = True
            logger.info(f"rest_exec.filled legs={len(results)} net={opp.net_profit_cents}c")
            return outcome

        # Caso 2: ambas KILL → ninguna ejecutó (ventana cerrada). Cero exposición.
        if all(s is LegState.KILL for s in states):
            logger.info("rest_exec.no_fill both_legs=KILL (ventana cerrada, sin exposición)")
            return outcome

        # Caso 3/4: hay asimetría o ERROR_RED → reconciliar y, si quedó exposición,
        # rollback de las patas efectivamente llenas.
        filled_legs = await self._resolve_exposure(results, outcome)
        if filled_legs:
            await self._rollback(filled_legs, outcome)
        return outcome

    # =====================================================
    # Ejecución de pata (FOK)
    # =====================================================

    async def _place_fok(self, leg: ArbLeg, coid: str) -> LegResult:
        """
        Coloca una pata con FOK. Resuelve a FILL / KILL / ERROR_RED.

        - FILL  : la respuesta indica la orden llena (status filled / count llenado).
        - KILL  : la respuesta indica que el FOK no llenó y se canceló.
        - ERROR_RED: excepción de red/timeout → estado DESCONOCIDO (no asumir KILL).
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
        except Exception as exc:
            # NO asumir KILL: la orden pudo llegar a Kalshi y llenarse.
            logger.warning(f"rest_exec.leg.error_red ticker={leg.market_ticker} side={leg.side}: {exc}")
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
                    logger.warning(f"rest_exec.reconcile ticker={r.leg.market_ticker} → FILLED (rollback)")
                    exposed.append(r)
                else:
                    logger.info(f"rest_exec.reconcile ticker={r.leg.market_ticker} → not filled")
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
            logger.critical(f"rest_exec.reconcile.get_orders_failed ticker={r.leg.market_ticker}: {exc} → assume exposed")
            return True

        # Fuente 2: get_positions, independiente del parsing de órdenes.
        try:
            has_position = await self._has_open_position(r.leg.market_ticker)
        except Exception as exc:
            logger.critical(f"rest_exec.reconcile.get_positions_failed ticker={r.leg.market_ticker}: {exc} → assume exposed")
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
        positions = resp.get("market_positions", resp.get("positions", [])) if isinstance(resp, dict) else []
        for p in positions:
            if str(p.get("ticker", "")) == ticker:
                # 'position' (contratos netos) != 0 → posición abierta.
                pos = _as_int(p.get("position"))
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
                    logger.info(f"rest_exec.rollback.filled ticker={leg.market_ticker} attempt={attempt + 1}")
                    return True
                logger.warning(
                    f"rest_exec.rollback.not_filled ticker={leg.market_ticker} "
                    f"attempt={attempt + 1}/{self.ROLLBACK_MAX_RETRIES}"
                )
            except Exception as exc:
                logger.warning(f"rest_exec.rollback.error ticker={leg.market_ticker} attempt={attempt + 1}: {exc}")
        return False

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
        """Pausa ya seteada por el caller; loggea CRITICAL (dispara alerta Telegram)."""
        tickers = [r.leg.market_ticker for r in filled_legs]
        msg = f"rest_exec.kill_switch posiciones_expuestas={tickers} rollback_no_convergió"
        logger.critical(msg)
        try:
            from src.monitoring.telegram_alerts import alert_error

            await alert_error(f"Motor REST KILL-SWITCH: rollback no se llenó, posición abierta {tickers}")
        except Exception:
            logger.exception("rest_exec.kill_switch.telegram_failed")

    @property
    def is_paused(self) -> bool:
        return self._paused
