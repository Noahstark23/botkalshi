"""Arbitrage executor for Motor 1 — concurrent order placement with atomic rollback."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlmodel import select

from src.clients.kalshi_rest import KalshiAPIError, KalshiRestClient
from src.math.arbitrage import ArbLeg, ArbOpportunity, select_hard_leg
from src.monitoring.health import BotState
from src.monitoring.telegram_alerts import alert_error, alert_risk_event
from src.risk.manager import RiskManager
from src.storage.models import RiskEvent, Trade, _naive_utc_now, engage_kill_switch, get_session
from src.strategies.data_capture import _top_bid, rest_orderbook_sides
from src.strategies.motor_1_arbitrage.event_exposure import EventExposureTracker
from src.utils.config import get_settings

if TYPE_CHECKING:
    pass

# Un FOK que no encuentra volumen vuelve como HTTP 409 con este error.code — es un KILL
# determinístico (la orden llegó y murió limpia, sin posición), NO un error de red.
# Verificado contra la API viva en motor_rest_arb (demo, 2026-06-05); mismo criterio acá:
# match estricto 409 + code conocido; cualquier otro error → estado DESCONOCIDO (queda
# pending → lo resuelve reconcile_pending_trades).
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


class ArbitrageExecutor:
    """
    Executes detected arbitrage opportunities with atomic rollback.

    Placement: asyncio.gather sends all leg orders concurrently.
    Rollback: sell-at-1¢ (limit) to consume any bid > 0 if a leg fails.
    Reconciliation: resolves "pending" trades older than 30s against Kalshi's
        order history on startup — handles post-crash orphaned states.

    Technical debt (CONTEXT.md section 11):
    - DB access is sync SQLite (aiosqlite migration deferred)
    - asyncio.gather has no per-order timeout; consider adding in future
    - Slippage estimate uses depth=5 best bid, not full fill simulation
    - get_orders in reconcile has no pagination (limit=100 hard cap)
    - reconcile not yet wired into runner.py boot (Fase 6 task)

    Kalshi API assumptions (verify if API changes):
    - GET /portfolio/orders response: {"orders": [{..., "client_order_id": ...,
      "order_id": ..., "status": "executed"|"canceled"|...}, ...]}
    - POST /portfolio/orders response: {"order": {"order_id": ...}}
    - GET /markets/{ticker}/orderbook response:
      {"orderbook": {"yes": [[price, size], ...], "no": [[price, size], ...]}}
      YES/NO lists sorted highest-bid first.
    """

    def __init__(
        self,
        rest_client: KalshiRestClient,
        risk_manager: RiskManager,
        *,
        max_slippage_pct: float = 10.0,
        max_rollback_retries: int = 3,
        rollback_window_minutes: int = 60,
        circuit_breaker_threshold: int = 3,
    ) -> None:
        self.client = rest_client
        self.risk_manager = risk_manager
        self.max_slippage_pct = max_slippage_pct
        self.max_rollback_retries = max_rollback_retries
        self.rollback_window_minutes = rollback_window_minutes
        self.circuit_breaker_threshold = circuit_breaker_threshold
        # Bug 2 (incidente 2026-07-07): guard de exposición direccional por EVENTO — los tickers
        # hermanos de un partido (…HOUWSH-HOU / …HOUWSH-WSH) se arbeaban independientes y los
        # residuales se acumulaban en la misma dirección hasta $135 sin que nadie los sumara.
        # LAZY: se construye en el primer execute() (get_settings ahí, no en __init__ — el
        # executor también se instancia en contextos sin env completo, ej. reconcile de boot).
        self.event_tracker: EventExposureTracker | None = None
        # Pausa LOCAL de Motor 1 (respuesta proporcional a huérfanas chicas, 2026-07-29):
        # runtime-only y por MOTOR — el resto del bot y Motor 3 (que gestiona la huérfana)
        # siguen operando. El kill-switch GLOBAL sigue existiendo para huérfanas grandes y
        # para la escalada por repetición.
        self._motor_paused: bool = False
        self._motor_pause_reason: str | None = None
        # Bug 1: caché del balance real de Kalshi (TTL corto) para el pre-check por arb sin
        # martillar la API. (monotonic_ts, balance_usd); se invalida tras cada place exitoso.
        self._balance_cache: tuple[float, float] | None = None
        # Auto-resume del breaker (2026-07-30): contador de reanudaciones del día (el tope
        # diario es la escalada — agotado, queda pausado hasta un humano).
        self._resumes_today = 0
        self._resumes_day: object = None
        self.BALANCE_CACHE_TTL_SEC = 5.0

    # =====================================================
    # Public interface
    # =====================================================

    async def initialize(self) -> None:
        """Run reconciliation on startup to resolve orphaned pending trades."""
        reconciled = await self._count_pending_trades()
        logger.info(f"ArbitrageExecutor: {reconciled} pending trade(s) found, reconciling")
        await self.reconcile_pending_trades()

    async def reconcile_pending_trades(self) -> None:
        """
        Resolve pending trades older than 30s against Kalshi's order list.

        Statuses after reconcile:
            "filled"        — Kalshi confirms executed
            "cancelled"     — Kalshi confirms canceled
            "error"         — Kalshi returned unexpected status
            "error_missing" — trade not found in Kalshi at all (critical)
        """
        cutoff = datetime.now(UTC) - timedelta(seconds=30)
        with get_session() as s:
            pending: list[Trade] = list(
                s.exec(
                    select(Trade).where(
                        Trade.status == "pending",
                        Trade.placed_at <= cutoff,
                    )
                ).all()
            )

        if not pending:
            return

        try:
            resp = await self.client.get_orders(limit=100)
        except Exception as exc:
            logger.error(f"reconcile_pending_trades: get_orders failed: {exc}")
            return

        kalshi_orders: list[dict] = resp.get("orders", [])
        by_coid: dict[str, dict] = {
            o["client_order_id"]: o for o in kalshi_orders if "client_order_id" in o
        }

        for trade in pending:
            kalshi_order = by_coid.get(trade.client_order_id)
            if kalshi_order is None:
                logger.critical(
                    f"reconcile: trade {trade.client_order_id} NOT found in Kalshi — "
                    "possible lost order"
                )
                self._update_trade_status(trade.client_order_id, "error_missing")
                await alert_error(
                    f"Trade {trade.client_order_id} (ticker={trade.ticker}) not found in "
                    "Kalshi orders during reconciliation. Manual review required."
                )
                continue

            kalshi_status = kalshi_order.get("status", "")
            if kalshi_status == "executed":
                with get_session() as s:
                    t = s.exec(
                        select(Trade).where(Trade.client_order_id == trade.client_order_id)
                    ).first()
                    if t:
                        t.status = "filled"
                        t.filled_at = _naive_utc_now()
                        t.kalshi_order_id = str(kalshi_order.get("order_id", ""))
                        s.add(t)
                        s.commit()
            elif kalshi_status == "canceled":
                self._update_trade_status(trade.client_order_id, "cancelled")
            else:
                logger.warning(
                    f"reconcile: trade {trade.client_order_id} has unexpected Kalshi "
                    f"status={kalshi_status!r}"
                )
                self._update_trade_status(trade.client_order_id, "error")

    async def execute(self, opp: ArbOpportunity) -> bool:
        """
        Execute an arbitrage opportunity.

        Returns:
            True  — either successfully filled (live) or dry-run (TRADING_ENABLED=false).
            False — risk check failed, or partial/full placement failure.
        """
        # Pausa LOCAL de Motor 1 (proporcional): corta ANTES de tocar red o riesgo. Es el
        # equivalente por-motor del kill-switch, para huérfanas chicas.
        if self._motor_paused:
            logger.info(
                f"ArbitrageExecutor: Motor 1 PAUSADO localmente — {self._motor_pause_reason}"
            )
            return False

        # Self-healing del breaker ANTES del risk check (que rechaza por is_paused): si la
        # ventana ya se vació y las condiciones se cumplen, este mismo intento despausa y
        # sigue. Con el flag off (default) es un no-op.
        self._maybe_auto_resume()
        decision = await self.risk_manager.check_pre_trade(opp)
        if not decision.approved:
            logger.info(f"ArbitrageExecutor: risk check rejected — {decision.reason}")
            return False

        settings = get_settings()
        if not settings.TRADING_ENABLED:
            logger.info(
                f"[DRY-RUN] Would execute arb: "
                f"count={decision.max_allowed_count} "
                f"net_profit={opp.net_profit_cents}¢ "
                f"tickers={[leg.market_ticker for leg in opp.legs]}"
            )
            return True

        count = decision.max_allowed_count

        # Auditoría rentabilidad 2026-07-07: el net del arb se validó al count DETECTADO,
        # pero el RiskManager puede RECORTAR el count — y el ceil del fee por trade sube
        # el fee/contrato al achicar (fee(1,50)=2c vs 1.75c teóricos): un arb marginal
        # net>0 a 100 contratos puede ser net<=0 a 1-3. Recomputar al count autorizado y
        # abortar si el arb recortado ya no es rentable (perdedor determinístico).
        from src.math.fees import kalshi_fee_cents

        resized_gross = (100 - sum(leg.price_cents for leg in opp.legs)) * count
        resized_fees = sum(kalshi_fee_cents(count, leg.price_cents) for leg in opp.legs)
        if resized_gross - resized_fees <= 0:
            logger.info(
                f"ArbitrageExecutor: arb ABORTADO por resize — net al count autorizado "
                f"{count} = {resized_gross - resized_fees}c <= 0 (el ceil del fee se come "
                f"el edge; detectado net={opp.net_profit_cents}c a count={opp.count})"
            )
            return False

        # Bug 2 — guard de exposición direccional por evento: si el evento YA arrastra residual
        # direccional sobre el cap (huérfanas/netting), NO se opera más ese evento. Un arb
        # completo netea a cero, así que se chequea la exposición ACUMULADA, no la del arb.
        if self.event_tracker is None:
            self.event_tracker = EventExposureTracker(
                max_exposure_usd=float(settings.MAX_EVENT_DIRECTIONAL_EXPOSURE_USD)
            )
        allowed, exposure_usd, event = self.event_tracker.check_new_arb(
            [leg.market_ticker for leg in opp.legs]
        )
        if not allowed:
            logger.warning(
                f"ArbitrageExecutor: arb RECHAZADO por exposición direccional del evento "
                f"{event}: ${exposure_usd:.2f} > cap "
                f"${self.event_tracker.max_exposure_usd:.2f} (guard Bug 2)"
            )
            if self.event_tracker.should_alert(event):
                try:  # best-effort: Telegram caído no debe romper el path de rechazo
                    await alert_risk_event(
                        "event_exposure_cap",
                        f"Evento {event} con exposición direccional ${exposure_usd:.2f} > cap "
                        f"${self.event_tracker.max_exposure_usd:.2f} — arbs sobre este evento "
                        "bloqueados hasta que el residual se cierre/settlee.",
                    )
                except Exception:
                    logger.exception("event_exposure_cap: alerta Telegram falló")
            return False

        # Bug 1 — pre-check de balance REAL antes de colocar CUALQUIER pata (van concurrentes:
        # el costo relevante es el del arb ENTERO + fees). Si Kalshi no tiene cash para ambas
        # patas, colocar la primera garantiza una huérfana cuando la segunda rebote con
        # insufficient_balance. Abort limpio: sin patas, sin rollback, sin risk_event.
        if not await self._balance_sufficient(opp, count):
            return False

        # HARD-FIRST (fix 2026-07-09): el arb es free-money COMPETITIVO — la resting volume
        # que el book V2 vio al detectar VUELA entre la detección y la ejecución (otros la
        # toman o la quote se retira). Disparar ambas patas a ciegas en paralelo hacía que la
        # líquida llenara y la fina KILL-eara (409) → partial fill → rollback → circuit breaker
        # (el "problema de patas" recurrente). En vez de eso se coloca PRIMERO la pata DURA
        # (cara/fina — la que empíricamente killa): la FOK secuenciada sobre ella ES el test
        # barato de profundidad viva. Si killa, no se compró nada, las fáciles NO se envían y
        # el arb se salta LIMPIO (cero exposición, cero rollback, cero breaker); solo si la
        # dura llena se disparan las fáciles. Espeja el guardarraíl ya probado en
        # motor_rest_arb (executor.py, #85); select_hard_leg es la fuente única. NO se agrega
        # pre-check de depth: sería un no-op (count = min(available_size) ya, por construcción).
        coids = {leg: str(uuid.uuid4()) for leg in opp.legs}
        self._persist_intents(opp, count, [coids[leg] for leg in opp.legs])

        hard_leg, other_legs = select_hard_leg(opp)

        # Paso 1: SOLO la pata dura, y esperarla.
        (hard_result,) = await asyncio.gather(
            self._place_leg(hard_leg, count, coids[hard_leg]), return_exceptions=True
        )
        hard_state, hard_fill = self._process_leg_result(
            hard_leg, coids[hard_leg], count, hard_result
        )

        if hard_state in ("killed", "error"):
            # La dura no abrió posición limpia → las fáciles NUNCA se envían: cero exposición.
            # 'killed' = skip limpio; 'error' = fila dura pending que resuelve el reconcile.
            # Este es el caso DOMINANTE que antes encadenaba partial→rollback→breaker.
            for leg in other_legs:
                self._update_trade_status(coids[leg], "cancelled")
            logger.info(
                f"ArbitrageExecutor: pata dura {hard_leg.market_ticker} ({hard_leg.side}) "
                f"{hard_state} → {len(other_legs)} pata(s) fácil(es) NO enviada(s) "
                "(cero exposición, sin rollback)"
            )
            return False

        filled: list[tuple[ArbLeg, str, int]] = [(hard_leg, coids[hard_leg], hard_fill)]

        if hard_state == "filled_partial":
            # La dura llenó PARCIAL (no debería con FOK): hay exposición pero el arb no puede
            # quedar simétrico → no se envían las fáciles y se rollbackea la dura.
            for leg in other_legs:
                self._update_trade_status(coids[leg], "cancelled")
            logger.critical(
                f"ArbitrageExecutor: pata dura {hard_leg.market_ticker} fill PARCIAL "
                f"{hard_fill}/{count} → rollback de la dura, fáciles no enviadas"
            )
            await self._execute_iterative_rollback(filled)
            return False

        # Paso 2: la dura LLENÓ completa → recién ahora las fáciles (paralelo ENTRE ELLAS; en
        # un arb binario es una sola). Con la dura ya adentro, un KILL de la fácil (rara: es la
        # líquida) sí deja exposición → rollback de lo lleno (mismo path #137, pérdida real).
        cheap_results = await asyncio.gather(
            *[self._place_leg(lg, count, coids[lg]) for lg in other_legs],
            return_exceptions=True,
        )
        failed = False
        for lg, res in zip(other_legs, cheap_results, strict=True):
            state, fc = self._process_leg_result(lg, coids[lg], count, res)
            if state in ("filled_full", "filled_partial"):
                filled.append((lg, coids[lg], fc))
            if state != "filled_full":
                failed = True

        if not failed:
            logger.info(
                f"ArbitrageExecutor: all {len(opp.legs)} legs filled (hard-first) — "
                f"net_profit={opp.net_profit_cents}¢"
            )
            return True

        logger.critical(
            f"ArbitrageExecutor: partial fill hard-first — {len(filled)}/{len(opp.legs)} "
            "legs filled, triggering rollback"
        )
        await self._execute_iterative_rollback(filled)
        return False

    def _process_leg_result(
        self, leg: ArbLeg, coid: str, count: int, result: object
    ) -> tuple[str, int]:
        """Resuelve el resultado de una pata FOK a (estado, fill_count real).

        Auditoría 2026-07-07 (P0): HTTP 200 = "orden aceptada", NO "llenada" — el fill REAL
        sale de fill_count. Estados:
          - 'filled_full'    (fill_count >= count): pata llena.
          - 'filled_partial' (0 < fill_count < count): no debería con FOK — defensivo; se
            registra el count REAL igual.
          - 'killed'         (409 determinístico de FOK sin volumen, o HTTP 200 con fill 0):
            sin posición → fila cancelled.
          - 'error'          (excepción no-kill): estado DESCONOCIDO → la fila queda pending y
            la resuelve reconcile_pending_trades. Jamás se asume que no pasó nada.
        Para los 'filled_*' hace el bookkeeping del fill (Trade row + event_tracker + invalida
        el cache de balance). El rollback lo decide el CALLER (hard-first)."""
        if isinstance(result, BaseException):
            if (
                isinstance(result, KalshiAPIError)
                and result.status_code == 409
                and result.error_code in _FOK_KILL_ERROR_CODES
            ):
                logger.warning(
                    f"ArbitrageExecutor: leg {leg.market_ticker} ({leg.side}) FOK killed "
                    "(sin volumen) — sin posición"
                )
                self._update_trade_status(coid, "cancelled")
                return "killed", 0
            logger.critical(
                f"ArbitrageExecutor: leg {leg.market_ticker} ({leg.side}) failed: {result}"
            )
            return "error", 0

        order = result.get("order", result) if isinstance(result, dict) else {}
        order_id = str(order.get("order_id", "") or result.get("order_id", ""))
        fill_count = _as_int(order.get("fill_count", order.get("fill_count_fp"))) or 0
        if fill_count <= 0:
            logger.warning(
                f"ArbitrageExecutor: leg {leg.market_ticker} ({leg.side}) FOK sin fill "
                "(killed) — sin posición"
            )
            self._update_trade_status(coid, "cancelled")
            return "killed", 0
        if fill_count < count:
            logger.warning(
                f"ArbitrageExecutor: fill PARCIAL inesperado con FOK "
                f"{leg.market_ticker} ({leg.side}): {fill_count}/{count}"
            )
        # Bug 2: alimentar el tracker con el fill REAL. Bug 1: el place consumió cash.
        self.event_tracker.record_fill(leg.market_ticker, leg.side, fill_count, leg.price_cents)
        self._balance_cache = None
        if not order_id:
            logger.warning(
                f"motor1.exec.order_id_ausente coid={coid} "
                f"response_keys={sorted(result)[:8]} (shape inesperado del place)"
            )
        with get_session() as s:
            t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
            if t:
                t.status = "filled"
                t.count = fill_count  # el count REAL fillado, no el pedido
                t.fill_price_cents = leg.price_cents
                t.filled_at = _naive_utc_now()
                if order_id:
                    t.kalshi_order_id = order_id
                s.add(t)
                s.commit()
        return ("filled_full" if fill_count >= count else "filled_partial"), fill_count

    # =====================================================
    # Internal helpers
    # =====================================================

    async def _execute_iterative_rollback(
        self,
        filled_legs: list[tuple[ArbLeg, str, int]],
    ) -> None:
        """Vende las patas filladas (IOC agresivo a 1¢) para salir de la posición.

        Auditoría 2026-07-07: (a) el sell va IOC — un fill parcial en una LIQUIDACIÓN es mejor
        que nada, y jamás queda resting; (b) se lee fill_count del sell y SOLO lo confirmado
        libera la posición; (c) la pérdida del rollback se REALIZA en pnl_cents (fila settled)
        → entra en los stop-losses diario/semanal/mensual, que antes eran ciegos a esto."""
        await self._check_circuit_breaker()

        for leg, original_coid, leg_count in filled_legs:
            remaining = leg_count
            for attempt in range(self.max_rollback_retries):
                if remaining <= 0:
                    break
                try:
                    ob = await self.client.get_orderbook(leg.market_ticker, depth=5)
                    # rest_orderbook_sides + _top_bid (shape 2026-07-15): el parser viejo
                    # leía vacío con el shape nuevo (rollback nunca encontraba bid) y su
                    # `entry[0] > 0` explotaba con precios dólares-string. _top_bid además
                    # toma el MEJOR bid con size>0 (la referencia correcta para vender),
                    # no el primer entry de la lista.
                    current_bid, _ = _top_bid(rest_orderbook_sides(ob)[leg.side])

                    if current_bid is None:
                        logger.warning(
                            f"rollback: no bid > 0 for {leg.market_ticker} ({leg.side}), "
                            f"attempt {attempt + 1}/{self.max_rollback_retries}"
                        )
                        await asyncio.sleep(1)
                        continue

                    slippage_pct = (leg.price_cents - current_bid) / leg.price_cents * 100
                    if slippage_pct > self.max_slippage_pct:
                        logger.critical(
                            f"rollback: slippage {slippage_pct:.1f}% > "
                            f"{self.max_slippage_pct}% for {leg.market_ticker} "
                            f"({leg.side}), bid={current_bid}¢ original={leg.price_cents}¢ — "
                            "aborting, manual intervention required"
                        )
                        await alert_error(
                            f"Rollback aborted: excessive slippage {slippage_pct:.1f}% "
                            f"on {leg.market_ticker} ({leg.side}). "
                            f"bid={current_bid}¢ original={leg.price_cents}¢. "
                            "Manual review required."
                        )
                        await self._pause_on_aborted_rollback(leg, slippage_pct, remaining)
                        break

                    logger.info(
                        f"rollback: selling {remaining} {leg.market_ticker} ({leg.side}) "
                        f"IOC at price=1 (best bid={current_bid}¢), attempt {attempt + 1}"
                    )
                    resp = await self.client.place_order(
                        ticker=leg.market_ticker,
                        side=leg.side,
                        action="sell",
                        count=remaining,
                        order_type="limit",
                        yes_price=1 if leg.side == "yes" else None,
                        no_price=1 if leg.side == "no" else None,
                        client_order_id=f"{original_coid}-rb{attempt}",
                        time_in_force="immediate_or_cancel",
                    )
                    order = resp.get("order", resp) if isinstance(resp, dict) else {}
                    sold = _as_int(order.get("fill_count", order.get("fill_count_fp"))) or 0
                    if sold <= 0:
                        logger.warning(
                            f"rollback: IOC sin fill para {leg.market_ticker} ({leg.side}) — "
                            f"reintento {attempt + 1}/{self.max_rollback_retries}"
                        )
                        await asyncio.sleep(1)
                        continue
                    sold = min(sold, remaining)
                    # SOLO lo confirmado libera posición y realiza pérdida. Precio contable:
                    # el bid leído (el IOC a 1¢ cruza desde el mejor bid; para sizes chicos es
                    # el precio efectivo — se anota como estimación en notes).
                    self._settle_rollback_fill(original_coid, leg, sold, current_bid)
                    remaining -= sold
                    if remaining > 0:
                        logger.warning(
                            f"rollback: fill parcial {sold}, quedan {remaining} "
                            f"{leg.market_ticker} ({leg.side}) — reintento"
                        )
                        await asyncio.sleep(1)
                        continue
                    break

                except Exception as exc:
                    logger.error(
                        f"rollback: exception for {leg.market_ticker} ({leg.side}), "
                        f"attempt {attempt + 1}/{self.max_rollback_retries}: {exc}"
                    )
                    await asyncio.sleep(1)
            if remaining > 0:
                # Lo no vendido sigue ABIERTO (fila filled con el remanente): visible como
                # exposición para el RiskManager y gestionable como huérfana por Motor 3.
                logger.critical(
                    f"rollback: {remaining} contratos de {leg.market_ticker} ({leg.side}) "
                    f"quedaron SIN cerrar tras {self.max_rollback_retries} intentos"
                )

    def _settle_rollback_fill(
        self, original_coid: str, leg: ArbLeg, sold: int, sell_price_cents: int
    ) -> None:
        """Realiza la pérdida del rollback en la fila BUY original (auditoría 2026-07-07: antes
        el rollback marcaba 'rolled_back' sin pnl_cents → la pérdida JAMÁS entraba en las
        ventanas de stop-loss, que solo agregan filas settled).

        Cierre total → la fila pasa a settled con pnl realizado. Parcial → se PARTE (mismo
        patrón que _settle_originals de M3): hija settled por lo vendido, la original conserva
        el remanente abierto (exposición real, reintentable/gestionable). Best-effort: un fallo
        de DB se loguea CRITICAL y no rompe el rollback (prioridad: seguir vendiendo)."""
        from src.math.fees import kalshi_fee_cents

        now = _naive_utc_now()
        # Fee del round-trip (entrada + salida): ya estaba descontado del PnL pero se
        # perdía sin registrar. Se guarda para que /stats/motors pueda medir el costo
        # de transacción del motor, no solo su resultado neto.
        fees = kalshi_fee_cents(sold, sell_price_cents) + kalshi_fee_cents(sold, leg.price_cents)
        pnl = sold * (sell_price_cents - leg.price_cents) - fees
        note = f"rollback_sell~{sell_price_cents}c"
        try:
            with get_session() as s:
                t = s.exec(select(Trade).where(Trade.client_order_id == original_coid)).first()
                if t is None:
                    logger.critical(f"rollback settle: fila {original_coid} no encontrada")
                    return
                if sold >= t.count:
                    t.status = "settled"
                    t.settled_at = now
                    t.pnl_cents = pnl
                    t.fees_cents = fees  # round-trip completo, reemplaza el estimado de entrada
                    t.notes = f"{t.notes or ''} {note}".strip()[:500]
                    s.add(t)
                else:
                    t.count = t.count - sold  # remanente sigue abierto (filled)
                    if t.fees_cents is not None:
                        # El tramo vendido se lleva su fee de entrada a la hija: sin esto
                        # el fee de esos contratos quedaría contado dos veces (patrón de
                        # _settle_originals en M3).
                        entry_tramo = kalshi_fee_cents(sold, leg.price_cents)
                        t.fees_cents = max(0, t.fees_cents - entry_tramo)
                    s.add(t)
                    s.add(
                        Trade(
                            client_order_id=f"{original_coid}-rbs{uuid.uuid4().hex[:8]}",
                            ticker=leg.market_ticker,
                            side=leg.side,
                            action="buy",
                            count=sold,
                            price_cents=leg.price_cents,
                            fill_price_cents=leg.price_cents,
                            strategy="motor_1_arbitrage",
                            status="settled",
                            settled_at=now,
                            pnl_cents=pnl,
                            fees_cents=fees,
                            notes=f"{t.notes or ''} {note} split".strip()[:500],
                        )
                    )
                s.commit()
        except Exception:
            logger.exception(f"rollback settle falló coid={original_coid} (pérdida sin realizar)")

    def _breaker_params(self) -> tuple[int, float, bool]:
        """(threshold, window_min, count_clean) desde settings — tunables EN VIVO (incidente
        2026-07-30: 3/60min hardcodeados mataron el día 1 del mes por $0.21). Fallback a los
        valores del ctor si los settings no cargan (tests viejos, contextos sin env)."""
        try:
            s = get_settings()
            return (
                int(s.MOTOR_1_BREAKER_THRESHOLD),
                float(s.MOTOR_1_BREAKER_WINDOW_MIN),
                bool(s.MOTOR_1_BREAKER_COUNT_CLEAN_ROLLBACKS),
            )
        except Exception:
            return self.circuit_breaker_threshold, float(self.rollback_window_minutes), True

    def _rollbacks_in_window(self, window_min: float, *, count_clean: bool) -> tuple[int, int]:
        """(contados_para_el_breaker, abortados) en la ventana. Los ABORTADOS (pata huérfana)
        siempre cuentan; los LIMPIOS (posición cerrada, pérdida en cents) solo si
        count_clean — el día 1 del mes, 3 rollbacks limpios de $0.21 totales costaron 12
        horas de bot muerto contándolos igual que a los peligrosos."""
        cutoff = datetime.now(UTC) - timedelta(minutes=window_min)
        with get_session() as s:
            clean = len(
                list(
                    s.exec(
                        select(RiskEvent).where(
                            RiskEvent.event_type == "atomic_rollback",
                            RiskEvent.triggered_at >= cutoff,
                        )
                    ).all()
                )
            )
            aborted = len(
                list(
                    s.exec(
                        select(RiskEvent).where(
                            RiskEvent.event_type == "rollback_aborted_slippage",
                            RiskEvent.triggered_at >= cutoff,
                        )
                    ).all()
                )
            )
        counted = aborted + (clean if count_clean else 0)
        return counted, aborted

    async def _check_circuit_breaker(self) -> None:
        """
        Record rollback event, then pause bot if threshold rollbacks occurred in window.

        Inserts the event FIRST so the count includes this rollback.
        """
        with get_session() as s:
            event = RiskEvent(
                event_type="atomic_rollback",
                severity="warning",
                message="Partial fill triggered rollback",
            )
            s.add(event)
            s.commit()

        threshold, window_min, count_clean = self._breaker_params()
        counted, _aborted = self._rollbacks_in_window(window_min, count_clean=count_clean)

        if counted >= threshold:
            BotState.is_paused = True
            BotState.pause_reason = (
                f"circuit_breaker: {counted}+ rollbacks in {window_min:.0f}min window"
            )
            logger.critical(
                f"Circuit breaker triggered: {counted} rollbacks in "
                f"{window_min:.0f}min — bot paused"
            )
            try:  # best-effort SIEMPRE: un fallo de Telegram/settings no rompe el freno
                await alert_risk_event(
                    "circuit_breaker",
                    f"{counted} rollbacks in {window_min:.0f}min. Bot paused. "
                    "Auto-resume al vaciarse la ventana si MOTOR_1_BREAKER_AUTO_RESUME=true "
                    "(condicionado: 0 abortados, tope diario); si no, POST /admin/resume.",
                )
            except Exception:
                logger.exception("circuit_breaker: alerta falló (la pausa igual quedó aplicada)")

    def _maybe_auto_resume(self) -> None:
        """Self-healing CONDICIONADO del breaker (incidente 2026-07-30: sin resume
        automático, 3 rollbacks limpios = 12 horas de mes muerto por $0.21; el único
        `is_paused = False` del árbol era el POST /admin/resume manual).

        SOLO despausa si TODAS: (1) MOTOR_1_BREAKER_AUTO_RESUME=true (default false —
        cambiar la respuesta de un freno es decisión del operador); (2) la pausa es DE ESTE
        breaker (pause_reason con el marcador — jamás toca kill-switch ni otras pausas);
        (3) la ventana ya está por debajo del umbral; (4) CERO rollbacks ABORTADOS en la
        ventana (huérfana = revisión humana, sin excepciones); (5) quedan reanudaciones en
        el tope diario (agotado el tope, queda pausado hasta un humano — la escalada)."""
        if not BotState.is_paused:
            return
        reason = BotState.pause_reason or ""
        if not reason.startswith("circuit_breaker"):
            return  # kill-switch u otra pausa: NUNCA se auto-levanta desde acá
        try:
            s = get_settings()
            if not bool(s.MOTOR_1_BREAKER_AUTO_RESUME):
                return
            max_per_day = int(s.MOTOR_1_BREAKER_MAX_RESUMES_PER_DAY)
        except Exception:
            return  # sin settings legibles no se despausa nada (fail-closed)
        threshold, window_min, count_clean = self._breaker_params()
        counted, aborted = self._rollbacks_in_window(window_min, count_clean=count_clean)
        if aborted > 0 or counted >= threshold:
            return
        today = datetime.now(UTC).date()
        if self._resumes_day != today:
            self._resumes_day, self._resumes_today = today, 0
        if self._resumes_today >= max_per_day:
            logger.warning(
                f"motor1.breaker.auto_resume_agotado ({self._resumes_today}/{max_per_day} "
                "hoy) — queda pausado hasta revisión humana (la escalada del self-healing)"
            )
            return
        self._resumes_today += 1
        BotState.is_paused = False
        BotState.pause_reason = None
        try:
            with get_session() as db:
                db.add(
                    RiskEvent(
                        event_type="breaker_auto_resume",
                        severity="warning",
                        message=(
                            f"ventana vacía ({counted}<{threshold} en {window_min:.0f}min, "
                            f"0 abortados) — resume {self._resumes_today}/{max_per_day} del día"
                        )[:500],
                    )
                )
                db.commit()
        except Exception:
            logger.exception("breaker_auto_resume: persistencia falló (resume igual aplicado)")
        logger.warning(
            f"motor1.breaker.AUTO_RESUME: ventana vacía — bot despausado "
            f"({self._resumes_today}/{max_per_day} del día)"
        )

    async def _balance_sufficient(self, opp: ArbOpportunity, count: int) -> bool:
        """
        Bug 1 — pre-check del balance REAL de Kalshi contra el costo TOTAL del arb (todas las
        patas + fees). FAIL-OPEN si el balance no se puede leer: el peor caso es el
        comportamiento previo (Kalshi rechaza con insufficient_balance) — no bloquear todo el
        motor por un hiccup de la API. También avisa (log warning) si el cash quedó por debajo
        del 10% de ACTIVE_CAPITAL_USD — señal de que el colchón real se está agotando.
        """
        from src.math.fees import kalshi_fee_cents

        total_cents = sum(
            count * leg.price_cents + kalshi_fee_cents(count, leg.price_cents) for leg in opp.legs
        )
        total_usd = total_cents / 100.0
        available = await self._available_balance_usd()
        if available is None:
            return True  # fail-open documentado: sin lectura, se intenta igual
        settings = get_settings()
        if available < settings.ACTIVE_CAPITAL_USD * 0.10:
            logger.warning(
                f"ArbitrageExecutor: balance real ${available:.2f} < 10% de "
                f"ACTIVE_CAPITAL_USD (${settings.ACTIVE_CAPITAL_USD:.0f}) — colchón agotándose"
            )
        if total_usd > available:
            logger.warning(
                f"ArbitrageExecutor: arb ABORTADO por balance — costo ${total_usd:.2f} "
                f"(ambas patas + fees) > cash disponible ${available:.2f}. Sin patas colocadas."
            )
            return False
        return True

    async def _available_balance_usd(self) -> float | None:
        """Balance real de Kalshi en USD, cacheado ~5s (invalidado tras cada place exitoso).
        None si la lectura falla (el caller decide fail-open)."""
        import time as _time

        now = _time.monotonic()
        if (
            self._balance_cache is not None
            and now - self._balance_cache[0] < self.BALANCE_CACHE_TTL_SEC
        ):
            return self._balance_cache[1]
        try:
            usd = float(await self.client.get_available_balance_usd())
        except Exception as exc:
            logger.warning(f"ArbitrageExecutor: get_available_balance falló: {exc} (fail-open)")
            return None
        self._balance_cache = (now, usd)
        return usd

    def _recent_aborted_rollbacks(self) -> int:
        """Abortos de rollback en las últimas 24h (de risk_events, así SOBREVIVE redeploys).
        Insumo de la escalada anti-acumulación: una huérfana chica REPETIDA no es un
        accidente, es un mercado roto — y ahí sí corresponde el kill-switch global."""
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        try:
            with get_session() as s:
                return len(
                    list(
                        s.exec(
                            select(RiskEvent).where(
                                RiskEvent.event_type == "rollback_aborted_slippage",
                                RiskEvent.triggered_at >= cutoff,
                            )
                        ).all()
                    )
                )
        except Exception:
            logger.exception("_recent_aborted_rollbacks: query falló → se asume escalada")
            return 10**6  # fail-CLOSED: si no se puede contar, se escala al global

    async def _pause_on_aborted_rollback(
        self, leg: ArbLeg, slippage_pct: float, remaining: int = 0
    ) -> None:
        """
        Bug 3 (incidente 2026-07-07): UN rollback abortado por slippage = PAUSA INMEDIATA
        y PERSISTENTE. Un abort significa que el mercado se movió tanto entre la pata y el
        rollback (−89% en el incidente) que hay evidencia estructural de cambio (in-play,
        liquidez, feed). Esperar 3 eventos (el circuit breaker genérico) acumula daño; y la
        pausa runtime-only se perdía con un Redeploy → el bot volvía a operar sobre el mismo
        mercado roto. engage_kill_switch persiste en operational_state → el boot re-hidrata
        la pausa (_rehydrate_kill_switch) y SOLO scripts/clear_kill_switch.py la levanta.
        Best-effort: un fallo de persistencia no rompe el rollback (prioridad: alertar).

        RESPUESTA PROPORCIONAL (2026-07-29, flag MOTOR_1_PROPORTIONAL_ORPHAN_PAUSE, default
        OFF): la evidencia de 21 días dice que los 3 abortos fueron de UN contrato cada uno
        (~$0.47) y cada uno paró el bot ENTERO 14+ horas. Con el flag on, una huérfana bajo
        MOTOR_1_ORPHAN_KILL_SWITCH_USD pausa SOLO Motor 1 (runtime; el resto de los motores
        y M3 —que gestiona la huérfana— siguen), y el kill-switch global se reserva para
        huérfanas grandes O para la N-ésima chica en 24h (anti-acumulación: repetido = roto).
        El RiskEvent y la alerta se emiten SIEMPRE, en los dos caminos.
        """
        exposure_usd = abs(remaining) * leg.price_cents / 100.0
        # FAIL-CLOSED EN TODA LA DECISIÓN (dos bugs cazados en QA 2026-07-29):
        #  (a) leer el flag por TRUTHINESS tomaba la rama BLANDA con un settings mockeado o a
        #      medias — fail-OPEN en un control de seguridad. Solo un True EXPLÍCITO ablanda.
        #  (b) `get_settings()` acá puede EXPLOTAR (el executor también vive en contextos sin
        #      env completo, ej. el reconcile de boot) y la excepción la traga el loop de
        #      rollback → la pausa NO ocurriría. Todo el cómputo va envuelto: cualquier fallo
        #      cae al kill-switch global histórico, que es el comportamiento seguro.
        local_only = False
        previous = 0
        limite = 1
        try:
            settings = get_settings()
            if settings.MOTOR_1_PROPORTIONAL_ORPHAN_PAUSE is True:
                umbral = settings.MOTOR_1_ORPHAN_KILL_SWITCH_USD
                if (
                    isinstance(umbral, (int, float))
                    and not isinstance(umbral, bool)
                    and exposure_usd < float(umbral)
                ):
                    crudo = settings.MOTOR_1_ORPHAN_ESCALATE_COUNT
                    # Sin un límite entero válido, `limite=1` → escala YA (nunca local).
                    limite = crudo if isinstance(crudo, int) and not isinstance(crudo, bool) else 1
                    # El evento de ESTE abort se persiste abajo, así que la escalada compara
                    # contra los PREVIOS: N-1 previos + este = N.
                    previous = self._recent_aborted_rollbacks()
                    local_only = previous + 1 < limite
        except Exception:
            logger.exception(
                "pause_on_aborted_rollback: no se pudo evaluar la política proporcional → "
                "kill-switch GLOBAL (fail-closed)"
            )
            local_only = False

        reason = (
            f"rollback_aborted_slippage: {leg.market_ticker} ({leg.side}) "
            f"slippage {slippage_pct:.1f}% — pata huérfana sin cerrar "
            f"({remaining} contratos ≈ ${exposure_usd:.2f})"
            + (
                " — pausa SOLO Motor 1 (proporcional; M3 gestiona la huérfana)"
                if local_only
                else " — revisión manual"
            )
        )
        if local_only:
            # Pausa LOCAL: Motor 1 deja de operar (execute() corta arriba), el resto del bot
            # sigue. Runtime-only a propósito: una huérfana de centavos no debe sobrevivir un
            # redeploy como bloqueo global — y si se repite, la escalada la convierte en uno.
            self._motor_paused = True
            self._motor_pause_reason = reason
            BotState.motor1_local_pause = reason  # visible en /status (freno que no se ve = bug)
        else:
            BotState.is_paused = True
            BotState.pause_reason = reason
        try:
            # El RiskEvent se graba SIEMPRE (los dos caminos): es el rastro forense y el
            # insumo de la escalada anti-acumulación.
            with get_session() as s:
                s.add(
                    RiskEvent(
                        event_type="rollback_aborted_slippage",
                        severity="warning" if local_only else "critical",
                        message=reason[:500],
                    )
                )
                s.commit()
            if not local_only:
                engage_kill_switch(reason)
        except Exception:
            logger.exception("pause_on_aborted_rollback: persistencia falló (pausa runtime activa)")
        if local_only:
            logger.critical(
                f"PAUSA LOCAL de Motor 1 por rollback abortado (proporcional): {reason} "
                f"[abortos previos en 24h: {previous}; escala al global en "
                f"{limite}]"
            )
        else:
            logger.critical(f"PAUSA PERSISTENTE por rollback abortado: {reason}")
        try:  # best-effort: un fallo de Telegram no debe romper el flujo del rollback
            await alert_risk_event(
                "rollback_aborted_slippage",
                (
                    f"{reason}. Motor 1 PAUSADO (runtime); el resto del bot sigue y Motor 3 "
                    f"gestiona la huérfana. Abortos en 24h: {previous + 1}/"
                    f"{limite} antes de escalar al kill-switch."
                    if local_only
                    else f"{reason}. Bot pausado PERSISTENTE (sobrevive redeploy); "
                    "levantar con scripts/clear_kill_switch.py tras revisar la pata huérfana."
                ),
            )
        except Exception:
            logger.exception("pause_on_aborted_rollback: alerta Telegram falló")

    def _persist_intents(
        self,
        opp: ArbOpportunity,
        count: int,
        client_ids: list[str],
    ) -> None:
        """Insert one Trade row per leg with status='pending' before touching network.

        arb_id COMPARTIDO en notes (bug producción 2026-07-02): el RiskManager solo
        netea arbs hedged si la fila trae 'arb_id=' en notes (manager.py) y el
        SettlementPoller agrupa por él (arb_group_key). Sin esto, un par hedged de
        riesgo neto ~$0 contaba su notional BRUTO como exposición direccional — el par
        243×243 del 30-jun copó $235 del cap compartido y dejó a Motor 2 en 0 contratos.
        """
        from src.math.fees import kalshi_fee_cents

        arb_id = str(uuid.uuid4())
        with get_session() as s:
            for leg, coid in zip(opp.legs, client_ids, strict=True):
                trade = Trade(
                    client_order_id=coid,
                    ticker=leg.market_ticker,
                    side=leg.side,
                    action="buy",
                    count=count,
                    price_cents=leg.price_cents,
                    # Fee de entrada estimado, como ya hacían M2 y REST al persistir la
                    # intención. Faltaba SOLO acá → los 519 trades settleados de M1
                    # reportaban fees $0.00 y un criterio "PnL/trade vs fee" lo aprobaba
                    # por comparar contra cero (2026-07-28). El PnL no cambia: el
                    # settlement ya descontaba este mismo fee vía fallback.
                    fees_cents=kalshi_fee_cents(count, leg.price_cents),
                    strategy="motor_1_arbitrage",
                    estimated_edge_pct=opp.edge_pct,
                    status="pending",
                    notes=f"arb_id={arb_id}",
                )
                s.add(trade)
            s.commit()

    def _update_trade_status(self, coid: str, status: str) -> None:
        """Update trade status (and filled_at if status='filled')."""
        with get_session() as s:
            trade = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
            if trade:
                trade.status = status
                if status == "filled":
                    trade.filled_at = _naive_utc_now()
                s.add(trade)
                s.commit()

    async def _place_leg(self, leg: ArbLeg, count: int, coid: str) -> dict:
        # FILL_OR_KILL obligatorio (auditoría 2026-07-07, P0): el default gtc dejaba la pata
        # RESTING (exposición silenciosa, Issue #14) y un fill parcial rompía la simetría del
        # arb. FOK = todo-o-nada por pata: fill_count == count o la orden muere limpia. El
        # propio docstring de place_order lo exige para arbitraje; REST arb ya lo cumplía.
        return await self.client.place_order(
            ticker=leg.market_ticker,
            side=leg.side,
            action="buy",
            count=count,
            order_type="limit",
            yes_price=leg.price_cents if leg.side == "yes" else None,
            no_price=leg.price_cents if leg.side == "no" else None,
            client_order_id=coid,
            time_in_force="fill_or_kill",
        )

    async def _count_pending_trades(self) -> int:
        """Count pending trades older than 30s (for initialize logging)."""
        cutoff = datetime.now(UTC) - timedelta(seconds=30)
        with get_session() as s:
            return len(
                list(
                    s.exec(
                        select(Trade).where(
                            Trade.status == "pending",
                            Trade.placed_at <= cutoff,
                        )
                    ).all()
                )
            )
