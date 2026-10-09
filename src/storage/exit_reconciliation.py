"""Conciliación contable explícita de una salida conocida, exclusivamente por GET.

No decide ventas, no abre/cancela órdenes, no levanta pausas y no recalcula filas
históricas ya liquidadas con estimaciones. El caller elige un intent persistido.
"""

from sqlmodel import select

from src.math.quantities import terminal_fill
from src.storage.exit_capture import collect_exit_execution
from src.storage.models import Trade, get_session
from src.storage.quantities import requested_quantity, validate_order_identity
from src.storage.reductions import REDUCTION_PREFIX, finalize_reduction


async def reconcile_known_exit(client, client_order_id: str):
    """Devuelve el resumen persistido o falla dejando el ledger anterior intacto."""
    with get_session() as session:
        row = session.exec(select(Trade).where(Trade.client_order_id == client_order_id)).one()
    if (
        row.status != "pending"
        or row.action != "sell"
        or not row.kalshi_order_id
        or row.strategy
        not in {"motor_1_arbitrage", "motor_rest_arb", "motor_2_consensus", "motor_3_clv"}
    ):
        raise ValueError("EXIT_RECONCILIATION_REQUIRES_KNOWN_PENDING_SELL")
    reduction = (row.notes or "").startswith(REDUCTION_PREFIX)
    if not reduction and row.strategy not in {"motor_2_consensus", "motor_3_clv"}:
        raise ValueError("EXIT_RECONCILIATION_ATTRIBUTION_UNSUPPORTED")
    response = await client.get_order(row.kalshi_order_id)
    order = response.get("order") if isinstance(response, dict) else None
    validate_order_identity(row, order)
    if (
        order.get("order_id") != row.kalshi_order_id
        or order.get("client_order_id") != row.client_order_id
        or order.get("ticker") != row.ticker
        or type(order.get("subaccount_number")) is not int
        or order["subaccount_number"] != 0
    ):
        raise ValueError("EXIT_RECONCILIATION_ORDER_IDENTITY_INVALID")
    outcome, book = ("no", "ask") if row.side == "yes" else ("yes", "bid")
    for key, expected in (("outcome_side", outcome), ("book_side", book)):
        if key in order and order[key] != expected:
            raise ValueError("EXIT_RECONCILIATION_DIRECTION_MISMATCH")
    if not (order.get("side") == row.side and order.get("action") == "sell") and not (
        order.get("outcome_side") == outcome and order.get("book_side") == book
    ):
        raise ValueError("EXIT_RECONCILIATION_DIRECTION_MISSING")
    sold = terminal_fill(order, requested_quantity(row))
    if sold is None or sold <= 0:
        raise ValueError("EXIT_RECONCILIATION_POSITIVE_TERMINAL_REQUIRED")
    execution = await collect_exit_execution(
        client,
        order_id=row.kalshi_order_id,
        ticker=row.ticker,
        side=row.side,
        quantity=sold,
    )
    # Los finalizadores releen y validan el SELL dentro de BEGIN IMMEDIATE.
    if reduction:
        finalize_reduction(row.client_order_id, sold, row.kalshi_order_id, execution=execution)
    else:
        from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

        Motor3ExitExecutor(client, strategy=row.strategy)._finalize_fill(
            row.client_order_id,
            row.ticker,
            row.side,
            sold,
            row.price_cents,
            row.kalshi_order_id,
            execution=execution,
        )
    return execution
