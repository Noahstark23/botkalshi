"""Cantidades del ledger compartidas: la columna legacy nunca es la verdad fraccionaria."""

from fractions import Fraction
from math import ceil
from typing import TYPE_CHECKING

from src.math.quantities import contract_count, fixed_count

if TYPE_CHECKING:
    from src.storage.models import Trade


def requested_quantity(row: object) -> Fraction:
    legacy = row.count
    if type(legacy) not in (int, Fraction) or contract_count(legacy) is None:
        raise ValueError("count legacy inválido")
    fixed = getattr(row, "count_fp", None)
    if fixed is None:
        return Fraction(legacy)
    parsed = contract_count(fixed) if isinstance(fixed, str) else None
    if parsed is None or ceil(parsed) != legacy:
        raise ValueError("count_fp inválido o contradictorio")
    return parsed


def quantity_fields(count: Fraction | int) -> dict:
    parsed = contract_count(count)
    if parsed is None:
        raise ValueError("cantidad inválida")
    # Un consumidor legacy sobre-reserva; nunca transforma 0.25 en cero.
    return {"count": ceil(parsed), "count_fp": fixed_count(parsed)}


def confirmed_fill(row: "Trade") -> Fraction | None:
    legacy = row.filled_count
    if legacy is not None and (type(legacy) is not int or legacy < 0):
        raise ValueError("filled_count legacy inválido")
    fixed = getattr(row, "filled_count_fp", None)
    if fixed is None:
        return Fraction(legacy) if legacy is not None else None
    if not isinstance(fixed, str):
        raise ValueError("filled_count_fp debe ser texto decimal")
    count = contract_count(fixed)
    if count is None or (legacy is not None and count != legacy):
        raise ValueError("cantidad persistida inválida o contradictoria")
    return count


def filled_quantity(row: "Trade") -> Fraction | None:
    """Fill confirmado; sólo filas históricas terminales permiten fallback legacy.

    Pending sin evidencia conserva None; una nueva fila con count_fp tampoco puede
    inventar un fill desde el intent. M5 siempre exige evidencia explícita.
    """
    filled = confirmed_fill(row)
    if filled is not None:
        if filled > requested_quantity(row):
            raise ValueError("fill excede la cantidad de la fila")
        return filled
    if (
        row.status in {"filled", "settled"}
        and row.strategy != "motor_5_mm"
        and getattr(row, "count_fp", None) is None
    ):
        return requested_quantity(row)
    return None


def record_fill(row: "Trade", count: Fraction | int) -> None:
    parsed = contract_count(count)
    before = confirmed_fill(row)
    if (
        parsed is None
        or parsed > requested_quantity(row)
        or (before is not None and parsed < before)
    ):
        raise ValueError("cantidad confirmada inválida o regresiva")
    row.filled_count_fp = fixed_count(parsed)
    row.filled_count = int(parsed) if parsed.denominator == 1 else None


def filled_fields(count: Fraction | int) -> dict:
    """Porción contable confirmada (incluye split); no usar para un intent sin fill."""
    parsed = contract_count(count)
    if parsed is None:
        raise ValueError("fill inválido")
    return {
        **quantity_fields(parsed),
        "filled_count_fp": fixed_count(parsed),
        "filled_count": int(parsed) if parsed.denominator == 1 else None,
    }


def resize_filled(row: "Trade", count: Fraction | int) -> None:
    """Split contable explícito; a diferencia del acumulado de una orden puede disminuir."""
    for key, value in filled_fields(count).items():
        setattr(row, key, value)


def open_quantity(row: "Trade") -> Fraction:
    value = filled_quantity(row)
    if value is None:
        raise ValueError("fila sin cantidad ejecutada confirmada")
    return value


def validate_order_identity(row: "Trade", order: object) -> None:
    """Los campos de correlación presentes deben pertenecer a la misma intención."""
    if not isinstance(order, dict):
        raise ValueError("respuesta de orden inválida")
    for field in ("client_order_id", "ticker", "side", "action"):
        if field in order and order[field] != getattr(row, field):
            raise ValueError(f"{field} contradictorio")
    order_id = order.get("order_id")
    if order_id is not None:
        if not isinstance(order_id, str) or not order_id:
            raise ValueError("order_id inválido")
        if row.kalshi_order_id is not None and row.kalshi_order_id != order_id:
            raise ValueError("order_id contradictorio")


def hold_uncertain_order(coid: str, order: object) -> None:
    """Conserva fill como piso sin reducir reserva; pausa durable ante orden no terminal."""
    from sqlalchemy import text
    from sqlmodel import select

    from src.math.quantities import api_count
    from src.monitoring.health import BotState
    from src.storage.models import Trade, engage_kill_switch, get_session
    from src.storage.order_costs import record_order_fill

    reason = f"Orden {coid} sin evidencia terminal; requiere reconciliación"
    BotState.is_paused = True
    BotState.pause_reason = reason
    engage_kill_switch(reason)
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        row = session.exec(select(Trade).where(Trade.client_order_id == coid)).one()
        if row.status != "pending":
            raise ValueError("evidencia incierta sobre fila ya finalizada")
        validate_order_identity(row, order)
        fill = api_count(order, "fill_count")
        if fill is not None and fill <= requested_quantity(row):
            if row.strategy == "motor_2_consensus" and row.action == "buy":
                record_order_fill(row, fill, order, legacy_estimates=True)
            else:
                record_fill(row, fill)
            if order.get("order_id"):
                row.kalshi_order_id = order["order_id"]
            session.add(row)
            session.commit()
