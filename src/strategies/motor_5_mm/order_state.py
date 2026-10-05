"""Evidencia de órdenes M5: cantidades exactas; una respuesta incompleta no libera riesgo."""

from __future__ import annotations

from fractions import Fraction

from src.math.quantities import contract_count

COUNT_QUARANTINE_NOTE = "[m5_count_quarantined]"


def parse_count(value: object) -> Fraction | None:
    """Fixed point exacto, hasta centésimas; más precisión exige cuarentena."""
    return contract_count(value)


def _count_field(order: dict, name: str) -> Fraction | None:
    fixed = f"{name}_fp"
    value = parse_count(order[fixed]) if fixed in order else parse_count(order.get(name))
    if value is None:
        return None
    # El campo nuevo manda, pero una contradicción con el legado exige conciliar.
    if fixed in order and name in order and parse_count(order[name]) != value:
        return None
    return value


def filled_count(order: object) -> Fraction | None:
    """Cantidad reportada sin convertir una omisión o fracción a cero."""
    return _count_field(order, "fill_count") if isinstance(order, dict) else None


def unrepresentable_fill(order: object, *, count: int) -> bool:
    """Evidencia explícita que no puede desaparecer con una lectura posterior.

    Un ack sin cantidades no acredita un fill ni un cero y sigue reintentable.
    Un campo presente con precisión no soportada, contradictorio o fuera del intent exige
    cuarentena durable; el caller primero debe correlacionar la identidad.
    """
    if not isinstance(order, dict) or not any(
        field in order for field in ("fill_count", "fill_count_fp")
    ):
        return False
    observed = filled_count(order)
    return observed is None or observed > count


def terminal_fill_count(
    order: object,
    *,
    order_id: str,
    client_order_id: str | None = None,
    count: int | None = None,
) -> Fraction | None:
    """Fill terminal confirmado por identidad; None conserva la incertidumbre.

    Un order_id exacto permite correlacionar respuestas antiguas sin coid. Si la
    respuesta sí incluye coid, no se tolera que contradiga la intención local.
    """
    if not isinstance(order, dict) or not order_id or order.get("order_id") != order_id:
        return None
    if (
        client_order_id is not None
        and "client_order_id" in order
        and order["client_order_id"] != client_order_id
    ):
        return None
    status = order.get("status")
    if not isinstance(status, str) or status not in {"canceled", "cancelled", "executed"}:
        return None
    filled = filled_count(order)
    if filled is None:
        return None
    if count is not None and parse_count(count) != count:
        return None
    if "initial_count" in order or "initial_count_fp" in order:
        initial = _count_field(order, "initial_count")
        if initial is None or (count is not None and initial != count):
            return None
        count = initial
    if ("remaining_count" in order or "remaining_count_fp" in order) and _count_field(
        order, "remaining_count"
    ) != 0:
        return None
    if count is not None and filled > count:
        return None
    if status == "executed" and (filled == 0 or (count is not None and filled != count)):
        return None
    return filled
