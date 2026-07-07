"""Arbitrage executor for Motor 1 — concurrent order placement with atomic rollback."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.monitoring.health import BotState
from src.monitoring.telegram_alerts import alert_error, alert_risk_event
from src.risk.manager import RiskManager
from src.storage.models import RiskEvent, Trade, engage_kill_switch, get_session
from src.strategies.motor_1_arbitrage.event_exposure import EventExposureTracker
from src.utils.config import get_settings

if TYPE_CHECKING:
    pass


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
        # Bug 1: caché del balance real de Kalshi (TTL corto) para el pre-check por arb sin
        # martillar la API. (monotonic_ts, balance_usd); se invalida tras cada place exitoso.
        self._balance_cache: tuple[float, float] | None = None
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
                        t.filled_at = datetime.now(UTC)
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

        client_order_ids = [str(uuid.uuid4()) for _ in opp.legs]

        self._persist_intents(opp, count, client_order_ids)

        tasks = [
            self._place_leg(leg, count, coid)
            for leg, coid in zip(opp.legs, client_order_ids, strict=True)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        filled: list[tuple[ArbLeg, str]] = []
        failed = False
        for leg, coid, result in zip(opp.legs, client_order_ids, results, strict=True):
            if isinstance(result, BaseException):
                logger.critical(
                    f"ArbitrageExecutor: leg {leg.market_ticker} ({leg.side}) failed: {result}"
                )
                failed = True
            else:
                filled.append((leg, coid))
                # Bug 2: alimentar el tracker de exposición con el fill (fuente rápida
                # intra-burst). Bug 1: el place consumió cash → invalidar caché de balance.
                self.event_tracker.record_fill(leg.market_ticker, leg.side, count, leg.price_cents)
                self._balance_cache = None
                # order_id: V2 lo anida en 'order'; fallback plano por si el shape varía
                # (las 60 filas del 30-jun quedaron con kalshi_order_id NULL — si esto
                # vuelve a salir vacío, el WARNING de abajo deja la evidencia).
                order_id = str(
                    result.get("order", {}).get("order_id", "") or result.get("order_id", "")
                )
                if not order_id:
                    logger.warning(
                        f"motor1.exec.order_id_ausente coid={coid} "
                        f"response_keys={sorted(result)[:8]} (shape inesperado del place)"
                    )
                with get_session() as s:
                    t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                    if t:
                        t.status = "filled"
                        t.filled_at = datetime.now(UTC)
                        if order_id:
                            t.kalshi_order_id = order_id
                        s.add(t)
                        s.commit()

        if not failed:
            logger.info(
                f"ArbitrageExecutor: all {len(opp.legs)} legs filled — "
                f"net_profit={opp.net_profit_cents}¢"
            )
            return True

        logger.critical(
            f"ArbitrageExecutor: partial fill — {len(filled)}/{len(opp.legs)} legs filled, "
            "triggering rollback"
        )
        await self._execute_iterative_rollback(filled, count)
        return False

    # =====================================================
    # Internal helpers
    # =====================================================

    async def _execute_iterative_rollback(
        self,
        filled_legs: list[tuple[ArbLeg, str]],
        count: int,
    ) -> None:
        """Sell filled legs aggressively (price=1) to exit position."""
        await self._check_circuit_breaker()

        for leg, original_coid in filled_legs:
            for attempt in range(self.max_rollback_retries):
                try:
                    ob = await self.client.get_orderbook(leg.market_ticker, depth=5)
                    orderbook = ob.get("orderbook", {})
                    bids = orderbook.get(leg.side, [])
                    current_bid = next((entry[0] for entry in bids if entry[0] > 0), None)

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
                        await self._pause_on_aborted_rollback(leg, slippage_pct)
                        break

                    logger.info(
                        f"rollback: selling {leg.market_ticker} ({leg.side}) "
                        f"at price=1 (best bid={current_bid}¢), attempt {attempt + 1}"
                    )
                    await self.client.place_order(
                        ticker=leg.market_ticker,
                        side=leg.side,
                        action="sell",
                        count=count,
                        order_type="limit",
                        yes_price=1 if leg.side == "yes" else None,
                        no_price=1 if leg.side == "no" else None,
                    )
                    self._update_trade_status(original_coid, "rolled_back")
                    break

                except Exception as exc:
                    logger.error(
                        f"rollback: exception for {leg.market_ticker} ({leg.side}), "
                        f"attempt {attempt + 1}/{self.max_rollback_retries}: {exc}"
                    )
                    await asyncio.sleep(1)

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

        cutoff = datetime.now(UTC) - timedelta(minutes=self.rollback_window_minutes)
        with get_session() as s:
            events: list[RiskEvent] = list(
                s.exec(
                    select(RiskEvent).where(
                        RiskEvent.event_type == "atomic_rollback",
                        RiskEvent.triggered_at >= cutoff,
                    )
                ).all()
            )

        if len(events) >= self.circuit_breaker_threshold:
            BotState.is_paused = True
            BotState.pause_reason = (
                f"circuit_breaker: {len(events)}+ rollbacks in "
                f"{self.rollback_window_minutes}min window"
            )
            logger.critical(
                f"Circuit breaker triggered: {len(events)} rollbacks in "
                f"{self.rollback_window_minutes}min — bot paused"
            )
            await alert_risk_event(
                "circuit_breaker",
                f"{len(events)} atomic rollbacks in {self.rollback_window_minutes}min. "
                "Bot paused. Manual review required.",
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

    async def _pause_on_aborted_rollback(self, leg: ArbLeg, slippage_pct: float) -> None:
        """
        Bug 3 (incidente 2026-07-07): UN rollback abortado por slippage = PAUSA INMEDIATA
        y PERSISTENTE. Un abort significa que el mercado se movió tanto entre la pata y el
        rollback (−89% en el incidente) que hay evidencia estructural de cambio (in-play,
        liquidez, feed). Esperar 3 eventos (el circuit breaker genérico) acumula daño; y la
        pausa runtime-only se perdía con un Redeploy → el bot volvía a operar sobre el mismo
        mercado roto. engage_kill_switch persiste en operational_state → el boot re-hidrata
        la pausa (_rehydrate_kill_switch) y SOLO scripts/clear_kill_switch.py la levanta.
        Best-effort: un fallo de persistencia no rompe el rollback (prioridad: alertar).
        """
        reason = (
            f"rollback_aborted_slippage: {leg.market_ticker} ({leg.side}) "
            f"slippage {slippage_pct:.1f}% — pata huérfana sin cerrar, revisión manual"
        )
        BotState.is_paused = True
        BotState.pause_reason = reason
        try:
            with get_session() as s:
                s.add(
                    RiskEvent(
                        event_type="rollback_aborted_slippage",
                        severity="critical",
                        message=reason[:500],
                    )
                )
                s.commit()
            engage_kill_switch(reason)
        except Exception:
            logger.exception("pause_on_aborted_rollback: persistencia falló (pausa runtime activa)")
        logger.critical(f"PAUSA PERSISTENTE por rollback abortado: {reason}")
        try:  # best-effort: un fallo de Telegram no debe romper el flujo del rollback
            await alert_risk_event(
                "rollback_aborted_slippage",
                f"{reason}. Bot pausado PERSISTENTE (sobrevive redeploy); "
                "levantar con scripts/clear_kill_switch.py tras revisar la pata huérfana.",
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
                    trade.filled_at = datetime.now(UTC)
                s.add(trade)
                s.commit()

    async def _place_leg(self, leg: ArbLeg, count: int, coid: str) -> dict:
        return await self.client.place_order(
            ticker=leg.market_ticker,
            side=leg.side,
            action="buy",
            count=count,
            order_type="limit",
            yes_price=leg.price_cents if leg.side == "yes" else None,
            no_price=leg.price_cents if leg.side == "no" else None,
            client_order_id=coid,
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
