"""Importes acumulados observados, ligados a la misma cantidad del ledger.

El caller correlaciona la orden y persiste cantidad, dinero y notas en una sola
transacción. El fallback legacy es opt-in y nunca crea importes FP observados.
"""

import re
from fractions import Fraction
from math import ceil

from src.math.fees import kalshi_fee_cents
from src.math.quantities import api_count
from src.storage.quantities import confirmed_fill, record_fill

COST_QUARANTINE_NOTE = "fill_cost_quarantined"
_MONEY = re.compile(r"[0-9]{1,15}(?:\.[0-9]{1,6})?")
_MONEY_FIELDS = (
    "maker_fees_dollars",
    "taker_fees_dollars",
    "maker_fill_cost_dollars",
    "taker_fill_cost_dollars",
)


def _sum_money(order: dict, fields: tuple[str, str]) -> Fraction | None:
    values = [order.get(field) for field in fields]
    # Ausencia/None no son cero. Un campo inválido no se oculta porque falte su par.
    if any(
        value is not None and (not isinstance(value, str) or not _MONEY.fullmatch(value))
        for value in values
    ):
        raise ValueError("importe observado inválido")
    if any(value is None for value in values):
        return None
    return sum((Fraction(value) for value in values), Fraction(0))


def _usd(value: Fraction) -> str:
    scaled = value * 1_000_000
    return f"{scaled.numerator // 1_000_000}.{scaled.numerator % 1_000_000:06d}"


def _clear_costs(row) -> None:
    row.fees_usd_fp = None
    row.fill_cost_usd_fp = None
    row.fees_cents = None
    row.fill_price_cents = None


def _unverified(row, count: Fraction) -> None:
    if count > 0 and "mm_cost_unverified" not in (row.notes or ""):
        row.notes = f"mm_cost_unverified | {row.notes or ''}"[:500]


def _quarantine(row, count: Fraction) -> None:
    _clear_costs(row)
    _unverified(row, count)
    # El marcador no se pierde por truncamiento ni se rehabilita en un replay.
    original = (row.notes or "").replace(COST_QUARANTINE_NOTE, "").strip(" |")
    row.notes = f"{COST_QUARANTINE_NOTE} | {original}".strip(" |")[:500]


def record_order_fill(
    row, count: Fraction | int, order: object, *, legacy_estimates: bool = False
) -> None:
    """Registra el fill válido aun cuando su dinero requiera revisión contable.

    Los pares maker/taker completos son totales, no precios por contrato. Una
    cantidad nueva invalida el dinero del acumulado anterior. Dinero presente
    inválido, contradictorio o regresivo queda en cuarentena durable: ningún
    replay posterior restaura automáticamente la evidencia monetaria.

    SELL conserva sólo fees: fill_cost no acredita ingreso frente a colateral.
    La ausencia de datos no activa cuarentena por sí sola ni inventa cero.
    """
    previous = confirmed_fill(row)
    had_costs = row.fill_cost_usd_fp is not None or row.fees_usd_fp is not None
    record_fill(row, count)
    count = confirmed_fill(row)
    assert count is not None
    if COST_QUARANTINE_NOTE in (row.notes or ""):
        _quarantine(row, count)
        return
    compatible = isinstance(order, dict) and api_count(order, "fill_count") == count
    if not compatible:
        if previous == count:
            return
        order = {}
    try:
        fees = _sum_money(order, ("maker_fees_dollars", "taker_fees_dollars"))
        gross = _sum_money(order, ("maker_fill_cost_dollars", "taker_fill_cost_dollars"))
        if fees is not None and fees > count:
            raise ValueError("fee excede cantidad ejecutada")
        if gross is not None and (gross > count or (count > 0 and gross == 0)):
            raise ValueError("coste incompatible con cantidad ejecutada")
        for field, value in (("fees_usd_fp", fees), ("fill_cost_usd_fp", gross)):
            old = getattr(row, field, None)
            if old is None:
                continue
            if not isinstance(old, str) or not _MONEY.fullmatch(old):
                raise ValueError("importe persistido inválido")
            if value is not None and (
                value < Fraction(old) or (previous == count and value != Fraction(old))
            ):
                raise ValueError("importe acumulado regresivo o contradictorio")
    except ValueError:
        _quarantine(row, count)
        return
    # ACK sin fill no acredita un coste de posición ni una comisión observada.
    if count == 0:
        fees = gross = None
    row.fees_usd_fp = _usd(fees) if fees is not None else None
    # Espejo legacy conservador: la verdad subcentavo permanece en la columna FP.
    row.fees_cents = ceil(fees * 100) if fees is not None else None
    row.fill_cost_usd_fp = _usd(gross) if gross is not None and row.action == "buy" else None
    row.fill_price_cents = None
    if row.fill_cost_usd_fp is not None:
        average_cents = gross * 100 / count
        if average_cents.denominator == 1:
            row.fill_price_cents = int(average_cents)
    if (
        legacy_estimates
        and compatible
        and count > 0
        and row.action == "buy"
        and not had_costs
        and not any(field in order for field in _MONEY_FIELDS)
    ):
        price = row.price_cents
        if type(price) is int and 1 <= price <= 99:
            row.fill_price_cents = price
            row.fees_cents = kalshi_fee_cents(count, price)
    if fees is None:
        _unverified(row, count)
