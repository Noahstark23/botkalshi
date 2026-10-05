"""Cantidades exactas en centésimas de contrato; ningún float entra al ledger."""

from decimal import Decimal, InvalidOperation
from fractions import Fraction

MAX_CONTRACT_UNITS = 2**63 - 1


def contract_count(value: object) -> Fraction | None:
    """None ante dato inválido; acepta hasta dos decimales sin redondear."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal, Fraction)):
        return None
    if isinstance(value, Fraction):
        scaled = value * 100
        if scaled.denominator != 1 or not 0 <= scaled <= MAX_CONTRACT_UNITS:
            return None
        return value
    text = str(value)
    if len(text) > 64:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or not 0 <= number <= MAX_CONTRACT_UNITS // 100:
        return None
    if number == 0:
        return Fraction(0)
    # Comprobar antes de multiplicar: un exponente diminuto no puede underflow a
    # cero bajo el contexto Decimal, ni un payload forzar potencias gigantes.
    if number.as_tuple().exponent < -2:
        return None
    scaled = number * 100
    if scaled != scaled.to_integral_value():
        return None
    return Fraction(int(scaled), 100)


def fixed_count(value: Fraction | int) -> str:
    parsed = contract_count(value)
    if parsed is None:
        raise ValueError("cantidad no representable en centésimas de contrato")
    units = int(parsed * 100)
    return f"{units // 100}.{units % 100:02d}"


def precise_usd(cents: Fraction | int) -> str:
    """Precio en centavos × contratos de 0.01 produce dólares de cuatro decimales."""
    units = Fraction(cents) * 100
    if units.denominator != 1:
        raise ValueError("importe no representable en diezmilésimas de dólar")
    signed = int(units)
    absolute = abs(signed)
    return f"{'-' if signed < 0 else ''}{absolute // 10000}.{absolute % 10000:04d}"


def signed_fixed_count(value: Fraction | int) -> str:
    return ("-" if value < 0 else "") + fixed_count(abs(value))
