"""Recuperación de intents por GET y contabilidad; nunca envía/cancela órdenes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlmodel import select

from src.math.fees import kalshi_fee_cents
from src.math.quantities import api_count, terminal_fill
from src.storage.models import Trade, _naive_utc_now, get_session
from src.storage.quantities import record_fill, requested_quantity, resize_filled
from src.storage.reductions import REDUCTION_PREFIX, finalize_reduction

RECOVERABLE_STRATEGIES = frozenset({"motor_1_arbitrage", "motor_2_consensus", "motor_rest_arb"})


@dataclass(frozen=True)
class ReconciliationResult:
    checked: int = 0
    resolved: int = 0
    unresolved: tuple[str, ...] = ()


async def reconcile_pending_orders(
    client, *, strategies: tuple[str, ...], min_age_seconds: int = 0
) -> ReconciliationResult:
    """Sólo procesa estrategias explícitas y conserva toda incertidumbre pendiente.

    El listado está limitado a 100 órdenes. Una ausencia, duplicado, status vivo,
    cantidad inválida o fallo de red nunca demuestra cero fills: el caller debe
    pausar entradas mientras ``unresolved`` no esté vacío. Los SELL de CLV quedan
    pendientes para su reconciliador, que conoce la atribución FIFO original.
    """
    if not set(strategies) <= RECOVERABLE_STRATEGIES:
        raise ValueError("estrategia sin reconciliador compatible")
    if not strategies:
        return ReconciliationResult()
    cutoff = datetime.now(UTC) - timedelta(seconds=min_age_seconds)
    with get_session() as s:
        pending = list(
            s.exec(
                select(Trade).where(
                    Trade.status == "pending",
                    Trade.strategy.in_(strategies),
                    Trade.placed_at <= cutoff,
                )
            )
        )
    if not pending:
        return ReconciliationResult()
    unresolved = {row.client_order_id for row in pending}
    try:
        response = await client.get_orders(limit=100)
        orders = response.get("orders")
        if not isinstance(orders, list) or any(not isinstance(o, dict) for o in orders):
            raise ValueError("listado de órdenes inválido")
    except Exception as exc:
        logger.error("Reconcile GET falló: {}", exc)
        return ReconciliationResult(len(pending), 0, tuple(sorted(unresolved)))

    for row in pending:
        matches = [o for o in orders if o.get("client_order_id") == row.client_order_id]
        if len(matches) != 1:
            continue
        order = matches[0]
        try:
            order_id = order.get("order_id")
            if row.kalshi_order_id and order_id and row.kalshi_order_id != order_id:
                raise ValueError("order_id contradictorio")
            for field in ("ticker", "side", "action"):
                if field in order and order[field] != getattr(row, field):
                    raise ValueError(f"{field} contradictorio")
            fill = api_count(order, "fill_count")
            status = order.get("status")
            if fill is None or fill > requested_quantity(row):
                continue
            terminal = terminal_fill(order, requested_quantity(row)) is not None
            if status == "executed" and fill == 0:
                continue
            if row.action == "sell":
                if terminal and (row.notes or "").startswith(REDUCTION_PREFIX):
                    finalize_reduction(row.client_order_id, fill, order_id)
                    unresolved.remove(row.client_order_id)
                continue
            if row.action != "buy":
                continue
            with get_session() as s:
                current = s.get(Trade, row.id)
                if current is None or current.status != "pending":
                    continue
                record_fill(current, fill)
                if terminal:
                    if fill > 0:
                        resize_filled(current, fill)
                        current.status = "filled"
                    else:
                        current.status = "cancelled"
                if fill > 0:
                    current.filled_at = _naive_utc_now()
                    current.fill_price_cents = current.price_cents
                    current.fees_cents = kalshi_fee_cents(fill, current.price_cents)
                if order_id:
                    current.kalshi_order_id = order_id
                s.add(current)
                s.commit()
            if terminal:
                unresolved.remove(row.client_order_id)
        except Exception as exc:
            logger.error("Reconcile {} inconcluso: {}", row.client_order_id, exc)
    return ReconciliationResult(
        len(pending), len(pending) - len(unresolved), tuple(sorted(unresolved))
    )
