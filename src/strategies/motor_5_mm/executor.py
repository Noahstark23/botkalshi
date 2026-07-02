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
  - Riesgo: cada orden nueva pasa por el headroom de exposición del RiskManager (la
    fila pending ya reserva capital para TODOS los motores). Reserva bilateral
    conservadora (decisión F2 documentada en el commit del RiskManager).
  - Todo se cotiza en el eje YES (como V2): bid = buy yes, ask = sell yes (que ABRE
    posición NO si llena — por eso el ask NO es un "sell protector" y el batch create
    del cliente lo gatea entero).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import ClassVar

from loguru import logger
from sqlmodel import col, select

from src.clients.kalshi_rest import (
    KalshiAPIError,
    KalshiClientError,
    KalshiRestClient,
)
from src.risk.manager import RiskManager
from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.quoter import QuoteSet

STRATEGY = "motor_5_mm"
COID_PREFIX = "m5mm-"


def _parse_count(value: object) -> int | None:
    """FixedPointCount de Kalshi ("5.00") → int. None si no es parseable."""
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


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
        self._risk = risk
        # Canary cap F3 (plan §5): techo ABSOLUTO del costo abierto del MM (pending +
        # filled), independiente del headroom global. None = sin cap propio (demo).
        self._max_exposure_usd = max_exposure_usd
        self._live: dict[str, dict[str, LiveOrder]] = {}  # ticker → {action: LiveOrder}
        self.corrupted: set[str] = set()  # tickers con estado en duda → reconcile primero

    def _lock(self, ticker: str) -> asyncio.Lock:
        return self._locks.setdefault(ticker, asyncio.Lock())

    def live_order_ids(self) -> list[str]:
        return [
            lo.kalshi_order_id
            for sides in self._live.values()
            for lo in sides.values()
            if lo.kalshi_order_id
        ]

    # =====================================================
    # Sync de quotes por tick
    # =====================================================

    async def sync_quotes(self, quote: QuoteSet) -> str:
        """Sincroniza las órdenes vivas del ticker con la quote deseada. Devuelve el
        desenlace ('synced' | 'corrupted' | 'risk_blocked' | 'error')."""
        ticker = quote.ticker
        async with self._lock(ticker):
            if ticker in self.corrupted:
                # Estado en duda: NO se cotiza encima. Cancel-all del ticker; si el
                # cancel sale limpio, el reconciler confirmará el estado real y
                # des-corromperá (acá seguimos sin cotizar este tick — conservador).
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
                    if current is not None:
                        await self._cancel(ticker, current)
                    continue
                price, count = want
                if current is not None and (current.price_cents, current.count) == want:
                    continue  # quote sin cambios → la orden sigue resting (maker queue)
                if current is not None:
                    ok = await self._cancel(ticker, current)
                    if not ok:
                        outcome = "corrupted"
                        continue  # ticker quedó corrupto: no colocar encima
                placed = await self._place(ticker, action, price, count)
                if placed is None:
                    outcome = "risk_blocked" if ticker not in self.corrupted else "corrupted"
            return outcome

    async def retire_ticker(self, ticker: str) -> None:
        """El ticker salió del universo (fair vencido / cap): cancela sus quotes vivas."""
        async with self._lock(ticker):
            await self._cancel_ticker(ticker, reason="left_universe")

    # =====================================================
    # Place / cancel de una orden
    # =====================================================

    async def _place(self, ticker: str, action: str, price: int, count: int) -> LiveOrder | None:
        cost_usd = price * count / 100.0
        if self._risk is not None and self._risk.exposure_headroom_usd() < cost_usd:
            logger.warning(
                f"motor5.exec.risk_blocked {ticker} {action} {count}@{price}c "
                f"(headroom < ${cost_usd:.2f})"
            )
            return None
        if self._max_exposure_usd is not None:
            open_cost = self._mm_open_cost_usd()
            if open_cost + cost_usd > self._max_exposure_usd:
                logger.warning(
                    f"motor5.exec.canary_cap {ticker} {action} {count}@{price}c "
                    f"(abierto ${open_cost:.2f} + ${cost_usd:.2f} > "
                    f"cap ${self._max_exposure_usd:.2f})"
                )
                return None
        coid = f"{COID_PREFIX}{uuid.uuid4()}"
        # INTENT PRE-RED: si el proceso muere entre el INSERT y la respuesta, la fila
        # pending existe y el reconciler la resuelve contra get_orders.
        try:
            with get_session() as s:
                s.add(
                    Trade(
                        client_order_id=coid,
                        ticker=ticker,
                        side="yes",
                        action=action,
                        count=count,
                        price_cents=price,
                        strategy=STRATEGY,
                        status="pending",
                        notes=f"mm_quote {action} (F2 demo)",
                    )
                )
                s.commit()
        except Exception:
            logger.exception(f"motor5.exec.intent_persist_error {ticker} → no se coloca")
            return None  # sin intent no hay orden (el capital no quedaría contado)
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
            # Rechazo DETERMINÍSTICO (4xx, p.ej. post_only cruzaría) → la orden NO existe.
            logger.info(f"motor5.exec.rejected {ticker} {action} {count}@{price}c: {exc}")
            self._mark_row(coid, "cancelled", note=f"rejected: {exc.error_code or exc.status_code}")
            return None
        except (KalshiAPIError, Exception):
            # AMBIGUO (timeout/5xx/red): la orden PUDO haber entrado. La fila queda
            # pending y el ticker corrupto → reconciler resuelve. Jamás re-colocar a ciegas.
            logger.exception(f"motor5.exec.place_ambiguous {ticker} {action} → corrupted")
            self.corrupted.add(ticker)
            return None
        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        kid = order.get("order_id") or resp.get("order_id")
        live = LiveOrder(coid, kid, action, price, count)
        self._live.setdefault(ticker, {})[action] = live
        self._set_kalshi_id(coid, kid)
        logger.info(f"motor5.exec.placed {ticker} {action} {count}@{price}c coid={coid[:13]}…")
        return live

    async def _cancel(self, ticker: str, live: LiveOrder) -> bool:
        """Cancela una orden viva. False = estado en duda (ticker queda corrupto)."""
        self._live.get(ticker, {}).pop(live.action, None)
        if live.kalshi_order_id is None:
            self.corrupted.add(ticker)  # nunca supimos su id → solo el reconciler sabe
            return False
        try:
            resp = await self._client.cancel_order(live.kalshi_order_id)
        except KalshiClientError as exc:
            # 4xx: la orden ya no está resting — pudo llenarse antes del cancel.
            logger.warning(f"motor5.exec.cancel_race {ticker} {live.action}: {exc} → reconcile")
            self.corrupted.add(ticker)
            return False
        except Exception:
            logger.exception(f"motor5.exec.cancel_ambiguous {ticker} → corrupted")
            self.corrupted.add(ticker)
            return False
        # Fill parcial ANTES del cancel: el response trae el order con su fill_count.
        order = resp.get("order", resp) if isinstance(resp, dict) else {}
        filled = _parse_count(order.get("fill_count")) or 0
        if filled > 0:
            self._mark_row(
                live.client_order_id, "filled", filled_count=filled, note="partial_then_cancel"
            )
        else:
            self._mark_row(live.client_order_id, "cancelled", note="quote_replaced")
        return True

    async def _cancel_ticker(self, ticker: str, *, reason: str) -> None:
        for live in list(self._live.get(ticker, {}).values()):
            await self._cancel(ticker, live)
        logger.info(f"motor5.exec.ticker_cleared {ticker} ({reason})")

    # =====================================================
    # Pánico: cancel-all (<5s — kill-switch / shutdown)
    # =====================================================

    async def cancel_all(self, reason: str) -> int:
        """Cancela TODAS las quotes vivas en 1-2 requests (batch; fallback secuencial).
        Los desenlaces finos (fills que ganaron la carrera) los verifica el reconciler."""
        ids = self.live_order_ids()
        coids = [lo.client_order_id for sides in self._live.values() for lo in sides.values()]
        tickers = list(self._live)
        self._live.clear()
        self.corrupted.update(tickers)  # todo queda en duda hasta el próximo reconcile
        if not ids:
            return 0
        try:
            await self._client.batch_cancel_orders(ids)
        except Exception:
            logger.exception("motor5.exec.batch_cancel_error → fallback secuencial")
            for oid in ids:
                try:
                    await self._client.cancel_order(oid)
                except Exception:
                    logger.warning(f"motor5.exec.cancel_all: {oid} no se pudo cancelar")
        for coid in coids:
            self._mark_row(coid, "cancelled", note=f"cancel_all: {reason[:50]}")
        logger.warning(f"motor5.exec.cancel_all n={len(ids)} reason={reason}")
        return len(ids)

    def _mm_open_cost_usd(self) -> float:
        """Costo abierto TOTAL del Motor 5 (pending reserva completo; filled usa
        filled_count). Fail-safe: error de DB → inf (ante la duda, el cap bloquea)."""
        try:
            with get_session() as s:
                rows = list(
                    s.exec(
                        select(Trade).where(
                            Trade.strategy == STRATEGY,
                            col(Trade.status).in_(["pending", "filled"]),
                        )
                    )
                )
        except Exception:
            logger.exception("motor5.exec.open_cost_error → cap bloquea (fail-safe)")
            return float("inf")
        total = 0
        for t in rows:
            count = (
                t.filled_count if t.status == "filled" and t.filled_count is not None else t.count
            )
            total += t.price_cents * count
        return total / 100.0

    # =====================================================
    # DB best-effort
    # =====================================================

    def _mark_row(
        self,
        coid: str,
        status: str,
        *,
        filled_count: int | None = None,
        note: str | None = None,
    ) -> None:
        try:
            with get_session() as s:
                row = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                if row is None:
                    return
                row.status = status
                if filled_count is not None:
                    row.filled_count = filled_count
                if note:
                    row.notes = f"{row.notes or ''} | {note}"[:500]
                s.add(row)
                s.commit()
        except Exception:
            logger.exception(f"motor5.exec.mark_row_error coid={coid} status={status}")

    def _set_kalshi_id(self, coid: str, kid: str | None) -> None:
        if kid is None:
            return
        try:
            with get_session() as s:
                row = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                if row is not None:
                    row.kalshi_order_id = kid
                    s.add(row)
                    s.commit()
        except Exception:
            logger.exception(f"motor5.exec.set_kid_error coid={coid}")
