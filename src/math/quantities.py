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


def signed_count(value: object) -> Fraction | None:
    if isinstance(value, str) and value.startswith("-"):
        parsed = contract_count(value[1:])
        return -parsed if parsed is not None else None
    if type(value) in (int, float) and value < 0:
        parsed = contract_count(-value)
        return -parsed if parsed is not None else None
    return contract_count(value)


def api_count(order: object, name: str, *, signed: bool = False) -> Fraction | None:
    """Prefiere _fp; null legacy no tapa el dato, una contradicción no inventa cero."""
    if not isinstance(order, dict):
        return None
    parse = signed_count if signed else contract_count
    fixed = order.get(f"{name}_fp")
    legacy = order.get(name)
    value = parse(fixed if f"{name}_fp" in order else legacy)
    if fixed is not None and legacy is not None and parse(legacy) != value:
        return None
    return value


def terminal_fill(order: object, requested, *, placement: bool = False) -> Fraction | None:
    """Cantidad FINAL comprobada, no sólo un fill acumulado mientras la orden sigue viva.

    Create V2 no incluye status: exige remaining_count=0 explícito. GET exige
    status terminal. Un status presente contradictorio jamás se ignora.
    Fuente: https://docs.kalshi.com/api-reference/orders/create-order-v2.md
    """
    fill = api_count(order, "fill_count")
    if fill is None or fill > requested:
        return None
    remaining = api_count(order, "remaining_count")
    has_remaining = any(key in order for key in ("remaining_count", "remaining_count_fp"))
    if has_remaining and remaining != 0:
        return None
    has_initial = any(key in order for key in ("initial_count", "initial_count_fp"))
    initial = api_count(order, "initial_count")
    if has_initial and initial != requested:
        return None
    if "status" not in order:
        return fill if placement and remaining == 0 else None
    if order["status"] not in {"executed", "canceled", "cancelled"}:
        return None
    if order["status"] == "executed" and (fill == 0 or (has_initial and fill != initial)):
        return None
    return fill
