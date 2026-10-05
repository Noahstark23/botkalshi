"""
Motor5Executor (F2) — quotes REALES: place/cancel con intents pre-red y lock por-ticker.

SOLO se construye bajo Capa A (TRADING_ENABLED AND MOTOR_MM_EXECUTION_ENABLED); en
producción el validador de config además rompe el boot con EXECUTION=true hasta F3 —
este executor corre contra DEMO (plan §4: F2 valida mecánica, no edge).

Principios (heredados de Motor REST / Motor 3):
  - INTENT PRE-RED: la fila Trade 'pending' se escribe ANTES de place_order. Un error
    ambiguo (timeout/5xx) deja la fila pending y marca el ticker CORRUPTO → el
    reconciler resuelve contra get_orders; jamás se asume "no pasó nada".
  - Lock por-ticker (ClassVar): dos ticks nunca mutan las quotes del mismo ticker a la
    vez.
  - Estado corrupto NO opera (Lección 9): un ticker corrupto primero cancela todo lo
    suyo y re-sincroniza; hasta entonces no se cotiza.
  - Riesgo: capital confirmado fresco, stops, sizing, exposición global y canary
    se verifican bajo el lock compartido del RiskManager; el intent se persiste
    antes de soltarlo y antes de la red. BUY reserva P y SELL reserva 100-P, sin
    neteo de inventario no atribuido. Quote completa o rechazo, nunca count parcial.
  - Todo se cotiza en el eje YES (como V2): bid = buy yes, ask = sell yes (que ABRE
    posición NO si llena — por eso el ask NO es un "sell protector" y el batch create
    del cliente lo gatea entero).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from fractions import Fraction
from typing import ClassVar

from loguru import logger
from sqlmodel import col, select

from src.clients.kalshi_rest import (
    KalshiAPIError,
    KalshiClientError,
    KalshiOrderConfirmationError,
    KalshiRestClient,
)
from src.risk.exposure import open_risk_cents
from src.risk.manager import RiskManager
from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.incidents import incident_status, latch_incident
from src.strategies.motor_5_mm.ledger import confirmed_fill, record_fill
from src.strategies.motor_5_mm.order_state import (
    COUNT_QUARANTINE_NOTE,
    filled_count,
    parse_count,
    terminal_fill_count,
    unrepresentable_fill,
)
from src.strategies.motor_5_mm.quoter import QuoteSet

STRATEGY = "motor_5_mm"
COID_PREFIX = "m5mm-"


def _parse_count(value: object) -> int | None:
    """Compatibilidad para consumidores existentes; nunca trunca una fracción."""
    return parse_count(value)


@dataclass(slots=True)
class LiveOrder:
    client_order_id: str
    kalshi_order_id: str | None
    action: str  # "buy" (bid) | "sell" (ask) — sobre el eje YES
    price_cents: int
    count: int


class Motor5Executor:
    """Mantiene las quotes reales de cada ticker sincronizadas con el QuoteSet del tick."""

    _locks: ClassVar[dict[str, asyncio.Lock]] = {}

    def __init__(
        self,
        client: KalshiRestClient,
        risk: RiskManager | None = None,
        *,
        max_exposure_usd: float | None = None,
    ) -> None:
        self._client = client
        self._risk = risk if risk is not None else RiskManager()
        # Canary cap F3 (plan §5): techo ABSOLUTO del costo abierto del MM (pending +
        # filled), independiente del headroom global. None = sin cap propio (demo).
        self._max_exposure_usd = max_exposure_usd
        self._live: dict[str, dict[str, LiveOrder]] = {}  # ticker → {action: LiveOrder}
        self.corrupted: set[str] = set()  # bloqueo local enclavado; conciliar no lo levanta
        self._cancel_fill_revision = 0

    def _lock(self, ticker: str) -> asyncio.Lock:
        return self._locks.setdefault(ticker, asyncio.Lock())

    def _latch_corruption(self, ticker: str, reason: str) -> None:
        self.corrupted.add(ticker)
        # Un fallo de persistencia se propaga al supervisor; el bloqueo local ya
        # existe y ninguna continuación del tick puede emitir el lado opuesto.
        latch_incident(f"executor {ticker}: {reason}")

    def live_order_ids(self) -> list[str]:
        return [
            lo.kalshi_order_id
            for sides in self._live.values()
            for lo in sides.values()
            if lo.kalshi_order_id
        ]

    def live_coids(self) -> set[str]:
        """Todas las quotes cuyo seguimiento conserva este proceso, incluso inciertas."""
        return {lo.client_order_id for sides in self._live.values() for lo in sides.values()}

    def managed_coids(self) -> set[str]:
        """Quotes sanas que pueden seguir resting sin un reintento de cancelación.

        Retener una quote incierta no equivale a mantenerla cotizando: excluir los
        tickers bloqueados permite que el conciliador reintente retirarlos incluso
        cuando kill-switch o salida del universo ya consumieron su primer intento.
        """
        return {
            live.client_order_id
            for ticker, sides in self._live.items()
            if ticker not in self.corrupted
            for live in sides.values()
        }

    def forget_confirmed(self, coids: set[str]) -> None:
        """Sólo el conciliador con evidencia terminal persistida puede liberar este dueño."""
        for ticker, sides in list(self._live.items()):
            for action, live in list(sides.items()):
                if live.client_order_id in coids:
                    del sides[action]
            if not sides:
                del self._live[ticker]

    # =====================================================
    # Sync de quotes por tick
    # =====================================================

    async def sync_quotes(self, quote: QuoteSet) -> str:
        """Sincroniza las órdenes vivas del ticker con la quote deseada. Devuelve el
        desenlace ('synced' | 'corrupted' | 'risk_blocked' | 'error')."""
        ticker = quote.ticker
        async with self._lock(ticker):
            inventory_revision = self._cancel_fill_revision
            if ticker in self.corrupted:
                # Estado en duda: NO se cotiza encima. La conciliación puede confirmar
                # el cierre, pero no levanta automáticamente este bloqueo enclavado.
                await self._cancel_ticker(ticker, reason="corrupted_resync")
                return "corrupted"
            desired: dict[str, tuple[int, int] | None] = {
                "buy": (quote.bid_cents, quote.size) if quote.bid_cents is not None else None,
                "sell": (quote.ask_cents, quote.size) if quote.ask_cents is not None else None,
            }
            outcome = "synced"
            for action, want in desired.items():
                current = self._live.get(ticker, {}).get(action)
                if want is None:
                    if current is not None and not await self._cancel(ticker, current):
                        return "corrupted"
                    continue
                price, count = want
                if current is not None and (current.price_cents, current.count) == want:
                    continue  # quote sin cambios → la orden sigue resting (maker queue)
                if current is not None:
                    ok = await self._cancel(ticker, current)
                    if not ok:
                        return "corrupted"  # tampoco emitir el lado opuesto del ticker
                if self._cancel_fill_revision != inventory_revision:
                    # La quote se calculó ANTES del cancel. Reconciliar y volver a
                    # calcular inventario/tamaño en el próximo tick antes de reemplazar.
                    return "risk_blocked"
                placed = await self._place(ticker, action, price, count)
                if placed is None:
                    outcome = "risk_blocked" if ticker not in self.corrupted else "corrupted"
                    if ticker in self.corrupted:
                        return "corrupted"
            return outcome

    async def retire_ticker(self, ticker: str) -> None:
        """El ticker salió del universo (fair vencido / cap): cancela sus quotes vivas."""
        async with self._lock(ticker):
            await self._cancel_ticker(ticker, reason="left_universe")

    # =====================================================
    # Place / cancel de una orden
    # =====================================================

    async def _place(self, ticker: str, action: str, price: int, count: int) -> LiveOrder | None:
        blocked, reason = incident_status()
        if blocked:
            self.corrupted.add(ticker)
            logger.warning(f"motor5.exec.incident_blocked {ticker}: {reason}")
            return None
        coid = f"{COID_PREFIX}{uuid.uuid4()}"
        intent = Trade(
            client_order_id=coid,
            ticker=ticker,
            side="yes",
            action=action,
            count=count,
            price_cents=price,
            strategy=STRATEGY,
            status="pending",
            notes=f"mm_quote {action}",
        )

        def persist_intent() -> bool:
            # El gate puede esperar el lock/stops: revalidar el latch al reservar.
            blocked, _ = incident_status()
            if blocked:
                self.corrupted.add(ticker)
                return False
            try:
                with get_session() as session:
                    session.add(intent)
                    session.commit()
                return True
            except Exception:
                logger.exception(f"motor5.exec.intent_persist_error {ticker} → no se coloca")
                self._latch_corruption(ticker, "intent_persist_failed")
                return False

        decision = await self._risk.check_quote_and_reserve(
            intent, persist_intent, max_exposure_usd=self._max_exposure_usd
        )
        if not decision.approved:
            logger.warning(f"motor5.exec.risk_blocked {ticker}: {decision.reason}")
            return None
        # La reserva y su dueño existen ANTES de la red, incluso si se cancela la
        # task o la respuesta no permite conocer el id del exchange.
        live = LiveOrder(coid, None, action, price, count)
        self._live.setdefault(ticker, {})[action] = live
        try:
            resp = await self._client.place_order(
                ticker=ticker,
                side="yes",
                action=action,
                count=count,
                yes_price=price,
                client_order_id=coid,
                time_in_force="gtc",
                post_only=True,
            )
        except KalshiClientError as exc:
            if exc.status_code in (408, 409):
                self._latch_corruption(ticker, f"place_ambiguous_http_{exc.status_code}")
                return None
            # Rechazo DETERMINÍSTICO (4xx, p.ej. post_only cruzaría) → la orden NO existe.
            logger.info(f"motor5.exec.rejected {ticker} {action} {count}@{price}c: {exc}")
            if not self._mark_row(
                coid, "cancelled", note=f"rejected: {exc.error_code or exc.status_code}"
            ):
                self._latch_corruption(ticker, "rejection_persist_failed")
            else:
                self.forget_confirmed({coid})
            return None
        except asyncio.CancelledError:
            self._latch_corruption(ticker, "place_task_cancelled")
            raise
        except (KalshiAPIError, Exception):
            # AMBIGUO (timeout/5xx/red): la orden PUDO haber entrado. La fila queda
            # pending y el ticker corrupto → reconciler resuelve. Jamás re-colocar a ciegas.
            logger.exception(f"motor5.exec.place_ambiguous {ticker} {action} → corrupted")
            self._latch_corruption(ticker, "place_ambiguous")
            return None
        order = resp.get("order", resp) if isinstance(resp, dict) else None
        kid = order.get("order_id") if isinstance(order, dict) else None
        if (
            not isinstance(resp, dict)
            or resp.get("error") is not None
            or not isinstance(order, dict)
            or order.get("error") is not None
            or not isinstance(kid, str)
            or not kid.strip()
            or resp.get("order_id", kid) != kid
            or order.get("client_order_id", coid) != coid
            or resp.get("client_order_id", coid) != coid
            or order.get("ticker", ticker) != ticker
            or order.get("action", action) != action
            or order.get("side", "yes") != "yes"
        ):
            self._latch_corruption(ticker, "place_response_invalid")
            return None
        live.kalshi_order_id = kid
        if not self._set_kalshi_id(coid, kid):
            self._latch_corruption(ticker, "order_id_persist_failed")
            return None
        logger.info(f"motor5.exec.placed {ticker} {action} {count}@{price}c coid={coid[:13]}…")
        return live

    async def _cancel(self, ticker: str, live: LiveOrder) -> bool:
        """Cancela una orden viva. False = estado en duda (ticker queda corrupto)."""
        if live.kalshi_order_id is None:
            self._latch_corruption(ticker, "cancel_without_order_id")
            return False
        try:
            resp = await self._client.cancel_order(live.kalshi_order_id)
        except asyncio.CancelledError as exc:
            try:
                self._preserve_error_observations(ticker, live, getattr(exc, "observed_orders", []))
                self._latch_corruption(ticker, "cancel_task_cancelled")
            except Exception:
                # El fallo durable ya revoca al dueño sano; no sustituir la
                # petición de cancelación por un error secundario de persistencia.
                logger.exception("motor5.cancel_evidence_failed_preserving_cancellation")
            raise
        except KalshiOrderConfirmationError as exc:
            observations = getattr(exc, "observed_orders", [])
            self._preserve_error_observations(
                ticker, live, [*observations, getattr(exc, "observed_order", None)]
            )
            self._latch_corruption(ticker, "cancel_confirmation_missing")
            return False
        except KalshiClientError as exc:
            # 4xx: la orden ya no está resting — pudo llenarse antes del cancel.
            self._preserve_error_observations(ticker, live, getattr(exc, "observed_orders", []))
            logger.warning(f"motor5.exec.cancel_race {ticker} {live.action}: {exc} → reconcile")
            self._latch_corruption(ticker, "cancel_race")
            return False
        except Exception as exc:
            self._preserve_error_observations(ticker, live, getattr(exc, "observed_orders", []))
            logger.exception(f"motor5.exec.cancel_ambiguous {ticker} → corrupted")
            self._latch_corruption(ticker, "cancel_ambiguous")
            return False
        order = (
            resp.get("order", resp) if isinstance(resp, dict) and resp.get("error") is None else {}
        )
        return self._confirm_cancel(ticker, live, order, note="quote_cancel")

    def _confirm_cancel(self, ticker: str, live: LiveOrder, order: object, *, note: str) -> bool:
        if self._quarantine_unrepresentable_order(ticker, live, order):
            return False
        filled = terminal_fill_count(
            order,
            order_id=live.kalshi_order_id or "",
            client_order_id=live.client_order_id,
            count=live.count,
        )
        if filled is None or not self._mark_row(
            live.client_order_id,
            "filled" if filled else "cancelled",
            filled_count=filled,
            note=note,
        ):
            # La reserva pending y el dueño sobreviven incluso a un HTTP 200 si
            # falta evidencia terminal o su persistencia: no hay cancel ficticio.
            self._latch_corruption(ticker, "cancel_unconfirmed")
            logger.warning(f"motor5.exec.cancel_unconfirmed {ticker} → conserva seguimiento")
            return False
        self.forget_confirmed({live.client_order_id})
        if filled:
            self._cancel_fill_revision += 1
        return True

    def _quarantine_unrepresentable_order(
        self, ticker: str, live: LiveOrder, order: object
    ) -> bool:
        """Usar evidencia de error sólo para bloquear, nunca para confirmar cierre."""
        if (
            isinstance(order, dict)
            and live.kalshi_order_id is not None
            and order.get("order_id") == live.kalshi_order_id
            and order.get("client_order_id", live.client_order_id) == live.client_order_id
            and unrepresentable_fill(order, count=live.count)
        ):
            try:
                with get_session() as session:
                    row = session.exec(
                        select(Trade).where(Trade.client_order_id == live.client_order_id)
                    ).first()
                    if row is None or row.strategy != STRATEGY or row.status != "pending":
                        raise RuntimeError("no se puede conservar cuarentena terminal M5")
                    if COUNT_QUARANTINE_NOTE not in (row.notes or ""):
                        row.notes = f"{COUNT_QUARANTINE_NOTE} | {row.notes or ''}"[:500]
                        session.add(row)
                        session.commit()
            except Exception:
                self._latch_corruption(ticker, "terminal_quarantine_persist_failed")
                raise  # no continuar con otra orden sin conservar esta evidencia
            self._latch_corruption(ticker, "terminal_fill_unrepresentable")
            return True
        return False

    def _preserve_error_observations(
        self, ticker: str, live: LiveOrder, observations: object
    ) -> None:
        """Guardar fills observados antes de un fallo; nunca confirmar status desde él."""
        if not isinstance(observations, list):
            return
        for order in observations:
            if (
                not isinstance(order, dict)
                or order.get("order_id") != live.kalshi_order_id
                or order.get("client_order_id", live.client_order_id) != live.client_order_id
            ):
                continue
            if self._quarantine_unrepresentable_order(ticker, live, order):
                continue
            observed = filled_count(order)
            if observed is None or observed == 0:
                continue
            try:
                with get_session() as session:
                    row = session.exec(
                        select(Trade).where(Trade.client_order_id == live.client_order_id)
                    ).first()
                    if row is None or row.strategy != STRATEGY or row.status != "pending":
                        raise RuntimeError("no se puede conservar fill observado M5")
                    record_fill(row, max(confirmed_fill(row) or 0, observed))
                    session.add(row)
                    session.commit()
            except Exception:
                self._latch_corruption(ticker, "observed_fill_persist_failed")
                raise

    async def _cancel_ticker(self, ticker: str, *, reason: str) -> None:
        for live in list(self._live.get(ticker, {}).values()):
            await self._cancel(ticker, live)
        remaining = len(self._live.get(ticker, {}))
        logger.info(f"motor5.exec.ticker_cancel {ticker} remaining={remaining} ({reason})")

    # =====================================================
    # Pánico: cancel-all (kill-switch / shutdown; latencia depende de la API)
    # =====================================================

    async def cancel_all(self, reason: str) -> int:
        """Intenta cancelar todas; cuenta únicamente estados terminales persistidos.

        El batch puede ser parcial: cada resultado se confirma por separado y el
        fallback conserva toda quote cuyo desenlace siga ambiguo.
        """
        pending = [
            (ticker, live) for ticker, sides in self._live.items() for live in sides.values()
        ]
        ids = [live.kalshi_order_id for _, live in pending if live.kalshi_order_id]
        self.corrupted.update(ticker for ticker, _ in pending)
        if not ids:
            return 0
        confirmed = 0
        batch_orders: dict[str, dict] = {}
        live_by_id = {live.kalshi_order_id: (ticker, live) for ticker, live in pending}
        try:
            response = await self._client.batch_cancel_orders(ids)
        except BaseException as exc:
            # Un lote puede observar una cantidad imposible antes de fallar en
            # otra orden. Nunca perder esa evidencia al entrar al fallback.
            try:
                observations = getattr(exc, "observed_orders", [])
                if isinstance(observations, list):
                    for observed in observations:
                        oid = observed.get("order_id") if isinstance(observed, dict) else None
                        target = live_by_id.get(oid) if isinstance(oid, str) else None
                        if target is not None:
                            self._preserve_error_observations(*target, [observed])
                latch_incident("cancelación batch M5 incompleta; revisar órdenes")
            except Exception:
                if not isinstance(exc, Exception):
                    logger.exception("motor5.batch_evidence_failed_preserving_cancellation")
                    raise exc from None
                raise
            if not isinstance(exc, Exception):
                raise
            logger.exception("motor5.exec.batch_cancel_error → fallback secuencial")
        else:
            items = response.get("orders") if isinstance(response, dict) else None
            duplicates: set[str] = set()
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("error") is not None:
                        # Guardar evidencia descartada por el cliente ANTES del
                        # fallback: su lectura posterior podría devolver cero.
                        oid = item.get("order_id")
                        target = live_by_id.get(oid) if isinstance(oid, str) else None
                        if target is not None:
                            self._preserve_error_observations(
                                *target,
                                [*item.get("observed_orders", []), item.get("observed_order")],
                            )
                        continue
                    order = item.get("order", item)
                    if not isinstance(order, dict):
                        continue
                    oid = order.get("order_id")
                    if not isinstance(oid, str) or not oid:
                        continue
                    if "order" in item and item.get("order_id", oid) != oid:
                        continue
                    if oid in batch_orders:
                        duplicates.add(oid)
                    batch_orders[oid] = order
            for oid in duplicates:
                del batch_orders[oid]
        for ticker, live in pending:
            order = batch_orders.get(live.kalshi_order_id or "")
            if (
                order is not None
                and self._confirm_cancel(ticker, live, order, note=f"cancel_all: {reason[:50]}")
            ) or await self._cancel(ticker, live):
                confirmed += 1
        logger.warning(
            f"motor5.exec.cancel_all confirmed={confirmed} remaining={len(self.live_coids())} "
            f"reason={reason}"
        )
        return confirmed

    def _mm_open_cost_usd(self) -> float:
        """Costo abierto TOTAL del Motor 5 (pending reserva completo; filled usa
        filled_count). Fail-safe: error de DB → inf (ante la duda, el cap bloquea)."""
        try:
            with get_session() as s:
                rows = list(
                    s.exec(
                        select(Trade).where(
                            Trade.strategy == STRATEGY,
                            col(Trade.status).notin_(["settled", "cancelled"]),
                        )
                    )
                )
            return sum(open_risk_cents(trade) for trade in rows) / 100.0
        except Exception:
            logger.exception("motor5.exec.open_cost_error → cap bloquea (fail-safe)")
            return float("inf")

    # =====================================================
    # DB: la confirmación del cancel depende del commit, no sólo de la API.
    # =====================================================

    def _mark_row(
        self,
        coid: str,
        status: str,
        *,
        filled_count: int | Fraction | None = None,
        note: str | None = None,
    ) -> bool:
        try:
            with get_session() as s:
                row = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                if row is None or row.strategy != STRATEGY:
                    return False
                if COUNT_QUARANTINE_NOTE in (row.notes or ""):
                    return False  # requiere revisión de evidencia no representable
                if filled_count is not None and (
                    filled_count > row.count
                    or (confirmed_fill(row) is not None and filled_count < confirmed_fill(row))
                ):
                    return False
                if row.status != "pending":
                    # Una lectura tardía nunca revierte settlement ni borra un fill
                    # previo. Sólo se tolera la confirmación idempotente exacta.
                    return row.status == status and (
                        confirmed_fill(row) == filled_count
                        or (status == "cancelled" and confirmed_fill(row) in (None, 0))
                    )
                row.status = status
                if filled_count is not None:
                    record_fill(row, filled_count)
                if note:
                    row.notes = f"{row.notes or ''} | {note}"[:500]
                s.add(row)
                s.commit()
                return True
        except Exception:
            logger.exception(f"motor5.exec.mark_row_error coid={coid} status={status}")
            return False

    def _set_kalshi_id(self, coid: str, kid: str | None) -> bool:
        if kid is None:
            return False
        try:
            with get_session() as s:
                row = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                if row is None or row.strategy != STRATEGY:
                    return False
                if row.kalshi_order_id not in (None, kid):
                    return False
                row.kalshi_order_id = kid
                s.add(row)
                s.commit()
                return True
        except Exception:
            logger.exception(f"motor5.exec.set_kid_error coid={coid}")
            return False
