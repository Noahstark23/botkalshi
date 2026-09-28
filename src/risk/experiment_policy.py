"""Política fail-closed para la prueba REAL acotada a un bank de referencia de USD 200.

No autoriza trading. Solo calcula límites deterministas en centavos.
La ejecución requiere además los gates existentes de Settings/RiskManager.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

CENT = Decimal("0.01")
REFERENCE_UNIT_USD = Decimal("2.00")
REFERENCE_BANK_USD = Decimal("200.00")
AGGREGATE_UNITS = 3


def _money(value: Decimal | float | int | str) -> Decimal:
    amount = Decimal(str(value))
    if amount < 0:
        raise ValueError("money value must be non-negative")
    return amount.quantize(CENT, rounding=ROUND_DOWN)


@dataclass(frozen=True, slots=True)
class ExperimentLimits:
    capital_usd: Decimal
    unit_usd: Decimal
    habitual_usd: Decimal
    max_per_operation_usd: Decimal
    max_per_thesis_usd: Decimal
    max_open_usd: Decimal
    max_daily_new_risk_usd: Decimal


def limits_for_capital(capital_usd: Decimal | float | int | str) -> ExperimentLimits:
    capital = _money(capital_usd)
    one_percent = (capital * Decimal("0.01")).quantize(CENT, rounding=ROUND_DOWN)
    unit = min(REFERENCE_UNIT_USD, one_percent)
    habitual = (unit / Decimal("2")).quantize(CENT, rounding=ROUND_DOWN)
    aggregate = min(
        (unit * AGGREGATE_UNITS).quantize(CENT, rounding=ROUND_DOWN),
        Decimal("6.00"),
    )
    return ExperimentLimits(
        capital_usd=capital,
        unit_usd=unit,
        habitual_usd=habitual,
        max_per_operation_usd=unit,
        max_per_thesis_usd=unit,
        max_open_usd=aggregate,
        max_daily_new_risk_usd=aggregate,
    )
