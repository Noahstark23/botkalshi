"""Recuperación de intents por GET y contabilidad; nunca envía/cancela órdenes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import text
from sqlmodel import select

from src.math.fees import kalshi_fee_cents
from src.math.quantities import api_count, terminal_fill
from src.storage.exit_capture import collect_exit_execution
from src.storage.exit_fills import ExitFillSummary
from src.storage.models import Trade, _naive_utc_now, get_session
from src.storage.order_costs import COST_QUARANTINE_NOTE, record_order_fill
from src.storage.quantities import (
    record_fill,
    requested_quantity,
    resize_filled,
    validate_order_identity,
)
from src.storage.reductions import REDUCTION_PREFIX, finalize_reduction

RECOVERABLE_STRATEGIES = frozenset({"motor_1_arbitrage", "motor_2_consensus", "motor_rest_arb"})


def _sell_fee_observation(order: dict) -> dict:
    # El acumulado gross de SELL puede representar colateral. Sólo sus fees y
    # cantidades tienen semántica útil aquí; no borrar una cuarentena anterior.
    return {
        key: order[key]
        for key in ("fill_count", "fill_count_fp", "maker_fees_dollars", "taker_fees_dollars")
        if key in order
    }


def _record_sell_observation(row: Trade, fill, order: dict) -> None:
    notes = row.notes
    record_order_fill(row, fill, _sell_fee_observation(order))
    if (
        (notes or "").startswith(REDUCTION_PREFIX)
        and COST_QUARANTINE_NOTE not in (row.notes or "")
        and row.notes == f"mm_cost_unverified | {notes}"[:500]
    ):
        # La ausencia de fees añade una nota genérica de coste no verificado.
        # Pending reduction necesita su referencia EXACTA para FIFO/recovery;
        # totales ausentes y floors permanecen en sus columnas, nunca se inventan.
        # Cuarentena/contradicciones jamás se retiran mediante esta restauración.
        row.notes = notes


def _pause_exit_accounting(coid: str) -> None:
    from src.monitoring.health import BotState
    from src.storage.models import engage_kill_switch, kill_switch_engaged

    reason = f"SELL {coid}: fills contables incompletos; requiere conciliación"
    if not BotState.is_paused:
        BotState.is_paused = True
        BotState.pause_reason = reason
    try:
        if not kill_switch_engaged()[0]:
            engage_kill_switch(reason)
    except Exception:
        logger.exception("exit_capture.pause_persist_failed")


def validate_sell_order_identity(row: Trade, order: object) -> None:
    """También cero-fill debe concordar antes de liberar un intent de venta."""
    validate_order_identity(row, order)
    outcome, book = ("no", "ask") if row.side == "yes" else ("yes", "bid")
    for key, expected in (("outcome_side", outcome), ("book_side", book)):
        if key in order and order[key] != expected:
            raise ValueError("EXIT_CAPTURE_DIRECTION_MISMATCH")
    if "subaccount_number" in order and (
        type(order["subaccount_number"]) is not int or order["subaccount_number"] != 0
    ):
        raise ValueError("EXIT_CAPTURE_SUBACCOUNT_MISMATCH")


def validate_pending_sell_response(coid: str, order: object) -> None:
    """Identidad antes de interpretar cualquier ACK, incluso cero-fill legacy.

    No cambia cantidades ni aprende un OID de una respuesta contradictoria.
    Un fallo conserva el intent pendiente y pausa antes de permitir otra venta.
    """
    try:
        with get_session() as session:
            row = session.exec(select(Trade).where(Trade.client_order_id == coid)).one()
            if row.status != "pending" or row.action != "sell":
                raise ValueError("EXIT_CAPTURE_REQUIRES_PENDING_SELL")
            validate_sell_order_identity(row, order)
    except Exception:
        _pause_exit_accounting(coid)
        raise


async def capture_terminal_sell_execution(
    client, coid: str, order: dict, *, placement: bool = False
) -> ExitFillSummary:
    """Piso durable antes del GET; toda incertidumbre mantiene pending y pausa.

    No envía ni cancela. El caller conserva la ruta cero-fill; sólo una venta
    terminal positiva puede producir ingresos observados para su finalizador.
    """
    try:
        with get_session() as session:
            session.exec(text("BEGIN IMMEDIATE"))
            row = session.exec(select(Trade).where(Trade.client_order_id == coid)).one()
            if row.status != "pending" or row.action != "sell":
                raise ValueError("EXIT_CAPTURE_REQUIRES_PENDING_SELL")
            validate_sell_order_identity(row, order)
            sold = terminal_fill(order, requested_quantity(row), placement=placement)
            if sold is None or sold <= 0:
                raise ValueError("EXIT_CAPTURE_POSITIVE_TERMINAL_REQUIRED")
            _record_sell_observation(row, sold, order)
            if order.get("order_id"):
                row.kalshi_order_id = order["order_id"]
            # Capturar escalares antes del commit: no depender de entidad expirada.
            order_id, ticker, side = row.kalshi_order_id, row.ticker, row.side
            quarantined = COST_QUARANTINE_NOTE in (row.notes or "")
            session.add(row)
            session.commit()
        if quarantined:
            raise ValueError("EXIT_CAPTURE_COSTS_QUARANTINED")
        if not order_id:
            raise ValueError("EXIT_CAPTURE_ORDER_ID_MISSING")
        return await collect_exit_execution(
            client,
            order_id=order_id,
            ticker=ticker,
            side=side,
            quantity=sold,
        )
    except BaseException:
        # Incluye cancelación del await: el piso ya confirmado sobrevive reinicio.
        _pause_exit_accounting(coid)
        raise


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
            if row.action == "sell":
                validate_sell_order_identity(row, order)
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
                if not (row.notes or "").startswith(REDUCTION_PREFIX):
                    continue
                # Un IOC cuyo ACK se perdió puede aparecer primero como resting.
                # Conservar ese piso ANTES de otro GET/reinicio: un cancel posterior
                # con menor fill jamás prueba cero. No cerrar la venta ni reducir la
                # reserva hasta confirmación terminal; costes SELL no se inventan.
                with get_session() as s:
                    s.exec(text("BEGIN IMMEDIATE"))
                    current = s.get(Trade, row.id)
                    if current is None or current.status != "pending":
                        continue
                    validate_sell_order_identity(current, order)
                    _record_sell_observation(current, fill, order)
                    if order_id:
                        current.kalshi_order_id = order_id
                    s.add(current)
                    s.commit()
                if terminal:
                    execution = None
                    if fill > 0:
                        execution = await capture_terminal_sell_execution(
                            client, row.client_order_id, order
                        )
                    finalize_reduction(row.client_order_id, fill, order_id, execution=execution)
                    unresolved.remove(row.client_order_id)
                continue
            if row.action != "buy":
                continue
            with get_session() as s:
                s.exec(text("BEGIN IMMEDIATE"))
                current = s.get(Trade, row.id)
                if current is None or current.status != "pending":
                    continue
                validate_order_identity(current, order)
                if current.strategy == "motor_2_consensus":
                    record_order_fill(current, fill, order, legacy_estimates=True)
                    if COST_QUARANTINE_NOTE in (current.notes or ""):
                        terminal = False  # Conservar reserva y piso confirmado; exige conciliación.
                else:
                    record_fill(current, fill)
                if terminal:
                    if fill > 0:
                        resize_filled(current, fill)
                        current.status = "filled"
                    else:
                        current.status = "cancelled"
                if fill > 0:
                    current.filled_at = _naive_utc_now()
                    if current.strategy != "motor_2_consensus":
                        current.fill_price_cents = current.price_cents
                        current.fees_cents = kalshi_fee_cents(fill, current.price_cents)
                if order_id:
                    current.kalshi_order_id = order_id
                s.add(current)
                s.commit()
            if terminal:
                unresolved.remove(row.client_order_id)
        except Exception as exc:
            if row.action == "sell":
                _pause_exit_accounting(row.client_order_id)
            logger.error("Reconcile {} inconcluso: {}", row.client_order_id, exc)
    return ReconciliationResult(
        len(pending), len(pending) - len(unresolved), tuple(sorted(unresolved))
    )
