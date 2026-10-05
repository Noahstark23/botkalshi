"""Puente entre evidencia fixed-point y filas legacy, sin convertir fracciones a cero."""

from fractions import Fraction
from typing import TYPE_CHECKING

from src.math.quantities import contract_count, fixed_count

if TYPE_CHECKING:
    from src.storage.models import Trade


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


def record_fill(row: "Trade", count: Fraction | int) -> None:
    parsed = contract_count(count)
    before = confirmed_fill(row)
    if parsed is None or parsed > row.count or (before is not None and parsed < before):
        raise ValueError("cantidad confirmada inválida o regresiva")
    row.filled_count_fp = fixed_count(parsed)
    # El campo entero no afirma una cantidad ficticia. Los lectores M5 deben usar
    # confirmed_fill; None en el campo legacy NO significa ejecución completa.
    row.filled_count = int(parsed) if parsed.denominator == 1 else None
