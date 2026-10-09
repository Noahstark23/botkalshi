"""Reparto contable de costes de entrada sin perder el total al cerrar porciones."""

import re
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor

from src.math.fees import kalshi_fee_cents
from src.storage.order_costs import COST_QUARANTINE_NOTE
from src.storage.quantities import open_quantity

_USD = re.compile(r"\d{1,15}(?:\.\d{1,6})?")


def usd_text(value: Fraction) -> str:
    scaled = value * 1_000_000
    if scaled.denominator != 1:
        raise ValueError("importe no representable en microUSD")
    units = int(scaled)
    return f"{'-' if units < 0 else ''}{abs(units) // 1_000_000}.{abs(units) % 1_000_000:06d}"


def _parse(value: object) -> Fraction:
    if not isinstance(value, str) or not _USD.fullmatch(value):
        raise ValueError("coste de entrada inválido")
    return Fraction(value)


def _portion(total: Fraction, closed: Fraction, available: Fraction) -> Fraction:
    # MicroUSD enteros a la porción cerrada; el remanente retiene todo residuo.
    # La suma es exacta aun si el prorrateo ideal tiene decimales periódicos.
    return Fraction(floor(total * closed / available * 1_000_000), 1_000_000)


@dataclass(frozen=True)
class EntryAllocation:
    gross_cents: Fraction
    fee_cents: Fraction
    closed_fields: dict
    remaining_fields: dict


def allocate_entry(row, closed: Fraction) -> EntryAllocation:
    """No muta el ledger. Conserva costes observados y espejos legacy coherentes."""
    if COST_QUARANTINE_NOTE in (row.notes or ""):
        raise ValueError("costes de entrada en cuarentena; requiere conciliación")
    available = open_quantity(row)
    if not 0 < closed <= available:
        raise ValueError("porción fuera del BUY atribuible")
    price = row.fill_price_cents or row.price_cents
    closed_fields = {}
    remaining_fields = {}
    fixed_cost = row.fill_cost_usd_fp
    if fixed_cost is not None:
        total = _parse(fixed_cost)
        if total > available:
            raise ValueError("coste bruto excede pago máximo")
        part = _portion(total, closed, available)
        closed_fields["fill_cost_usd_fp"] = usd_text(part)
        remaining_fields["fill_cost_usd_fp"] = usd_text(total - part)
        gross_cents = part * 100
    else:
        gross_cents = closed * price

    fixed_fee = row.fees_usd_fp
    if fixed_fee is not None:
        total = _parse(fixed_fee)
        legacy = row.fees_cents
        if legacy is not None and legacy not in (floor(total * 100), ceil(total * 100)):
            raise ValueError("fee observada contradice espejo legacy")
        part = _portion(total, closed, available)
        fee_cents = part * 100
        closed_fields.update(fees_usd_fp=usd_text(part), fees_cents=ceil(fee_cents))
        remaining_fields.update(
            fees_usd_fp=usd_text(total - part), fees_cents=ceil((total - part) * 100)
        )
    else:
        total = row.fees_cents
        if total is None:
            total = kalshi_fee_cents(available, price)
        if type(total) is not int or total < 0:
            raise ValueError("fee de entrada inválida")
        # Compatibilidad legacy: nunca desechar fee remanente en el cierre final.
        fee_cents = Fraction(
            total if closed == available else min(total, kalshi_fee_cents(closed, price))
        )
        closed_fields["fees_cents"] = int(fee_cents)
        remaining_fields["fees_cents"] = total - int(fee_cents)
    return EntryAllocation(gross_cents, fee_cents, closed_fields, remaining_fields)
