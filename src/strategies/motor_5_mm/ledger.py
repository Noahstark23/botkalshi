"""Cantidades compartidas y costes acumulados observados de las órdenes M5."""

import re
from fractions import Fraction
from math import ceil

from src.storage.quantities import confirmed_fill, record_fill
from src.strategies.motor_5_mm.order_state import filled_count

__all__ = ["confirmed_fill", "record_fill", "record_order_fill"]

_MONEY = re.compile(r"\d{1,15}(?:\.\d{1,6})?")


def _sum_money(order: dict, fields: tuple[str, str]) -> Fraction | None:
    values = [order.get(field) for field in fields]
    if any(not isinstance(value, str) or not _MONEY.fullmatch(value) for value in values):
        return None
    return sum((Fraction(value) for value in values), Fraction(0))


def _usd(value: Fraction) -> str:
    scaled = value * 1_000_000
    return f"{scaled.numerator // 1_000_000}.{scaled.numerator % 1_000_000:06d}"


def record_order_fill(row, count: Fraction | int, order: object) -> None:
    """Conserva dinero de la MISMA cantidad acumulada, sin estimarlo desde tarifas.

    Contrato primario: https://docs.kalshi.com/api-reference/orders/get-order .
    Los pares maker/taker deben estar completos. Ausencia no significa cero; una
    cantidad nueva invalida el coste del acumulado anterior. Para SELL los campos
    'fill_cost' no documentan ingreso vs colateral: sólo se conserva su fee, nunca
    se convierte ese coste ambiguo en un precio de venta confirmado.
    """
    previous = confirmed_fill(row)
    record_fill(row, count)
    if not isinstance(order, dict) or filled_count(order) != count:
        if previous == count:
            return
        order = {}
    fees = _sum_money(order, ("maker_fees_dollars", "taker_fees_dollars"))
    gross = _sum_money(order, ("maker_fill_cost_dollars", "taker_fill_cost_dollars"))
    if fees is not None and (count <= 0 or fees > count):
        fees = None
    if gross is not None and (count <= 0 or not 0 < gross <= count):
        gross = None
    for field, value in (("fees_usd_fp", fees), ("fill_cost_usd_fp", gross)):
        old = getattr(row, field, None)
        if old is None or value is None:
            continue
        if not isinstance(old, str) or not _MONEY.fullmatch(old):
            value = None
        elif value < Fraction(old) or (previous == count and value != Fraction(old)):
            value = None  # Acumulado regresivo/contradictorio: exige nueva evidencia.
        if field == "fees_usd_fp":
            fees = value
        else:
            gross = value
    # No reutilizar datos de una cantidad anterior ni una respuesta inválida.
    row.fees_usd_fp = _usd(fees) if fees is not None else None
    # Los lectores legacy sólo admiten cents enteros: cota conservadora, jamás
    # un cero ficticio para una fee positiva subcent. MTM usa la columna exacta.
    row.fees_cents = ceil(fees * 100) if fees is not None else None
    row.fill_cost_usd_fp = _usd(gross) if gross is not None and row.action == "buy" else None
    row.fill_price_cents = None
    if row.fill_cost_usd_fp is not None:
        average_cents = gross * 100 / count
        if average_cents.denominator == 1:
            row.fill_price_cents = int(average_cents)
    if count > 0 and fees is None and "mm_cost_unverified" not in (row.notes or ""):
        row.notes = f"mm_cost_unverified | {row.notes or ''}"[:500]
