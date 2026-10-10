"""Fee de Kalshi para el simulador y la conciliación: dos modelos, ambos explícitos.

Por qué dos modelos (verificado 2026-10-09 contra el fee schedule oficial,
"Last updated and effective: July 7, 2026"):

  fórmula   fee = round up(M × 0.07 × C × P × (1−P))      (taker)
            fee = round up(M × 0.0175 × C × P × (1−P))    (maker)
  redondeo  "rounds up such that the fee + positionCost is rounded to a centicent"

Un centicent es $0.0001. `src.math.fees.kalshi_fee_cents` redondea al CENTAVO
entero ($0.01): es una cota superior conservadora (sobreestima hasta 1¢ por
orden — a 1 contrato y 50¢ cobra 2¢ cuando el schedule da $0.0175). Producción
la mantiene a propósito (fail-safe: el edge se subestima, nunca se infla). El
simulador registra AMBAS para que la diferencia sea medible y la conciliación
pueda decidir, con fills reales (`fee_cost`), cuál reproduce al exchange.

Ceil POR ORDEN en ambos modelos: C es el count de la orden/fill, nunca 1
multiplicado después (medir a count=1 sobreestima por contrato).

Puro: sin red, sin DB, sin cliente de órdenes.
"""

from __future__ import annotations

from fractions import Fraction

from src.math.fees import kalshi_fee_cents, kalshi_maker_fee_cents

FEE_MODEL_EXACT = "KALSHI_SCHEDULE_CENTICENT"
FEE_MODEL_CONSERVATIVE = "REPO_CEIL_CENT"

# 0.07 y 0.0175 como fracciones exactas.
_RATE = {"taker": Fraction(7, 100), "maker": Fraction(7, 400)}


def _validate(count: int, price_cents: int, fee_type: str) -> None:
    if type(count) is not int or count < 0:
        raise ValueError(f"count debe ser int >= 0, got {count!r}")
    if type(price_cents) is not int or not 0 <= price_cents <= 100:
        raise ValueError(f"price_cents debe ser int en 0..100, got {price_cents!r}")
    if fee_type not in _RATE:
        raise ValueError(f"fee_type debe ser 'taker' o 'maker', got {fee_type!r}")


def _multiplier(value: int | str | Fraction) -> Fraction:
    if isinstance(value, (bool, float)):
        # float metería error binario antes del ceil; se exige exacto.
        raise ValueError("fee_multiplier debe ser int, str decimal o Fraction (no float)")
    result = value if isinstance(value, Fraction) else Fraction(str(value))
    if result < 0:
        raise ValueError(f"fee_multiplier debe ser >= 0, got {value!r}")
    return result


def fee_centicents_exact(
    count: int,
    price_cents: int,
    *,
    fee_type: str = "taker",
    fee_multiplier: int | str | Fraction = 1,
) -> int:
    """Fee del schedule en centicents ($0.0001), ceil por orden.

    Con P = price_cents/100 en dólares:
        fee_usd = M × rate × C × P × (1−P)
        fee_cc  = ceil(fee_usd × 10_000)
    Para precio en centavos enteros, positionCost ya es múltiplo de centicent,
    así que redondear fee+positionCost a centicent equivale a redondear el fee.
    """
    _validate(count, price_cents, fee_type)
    m = _multiplier(fee_multiplier)
    if count == 0 or price_cents in (0, 100) or m == 0:
        return 0
    p = Fraction(price_cents, 100)
    fee_cc = m * _RATE[fee_type] * count * p * (1 - p) * 10_000
    return -((-fee_cc.numerator) // fee_cc.denominator)


def fee_cents_conservative(
    count: int,
    price_cents: int,
    *,
    fee_type: str = "taker",
    fee_multiplier: int | str | Fraction = 1,
) -> int:
    """La misma fee redondeada al centavo entero — delega en la función de producción."""
    _validate(count, price_cents, fee_type)
    m = _multiplier(fee_multiplier)
    fn = kalshi_fee_cents if fee_type == "taker" else kalshi_maker_fee_cents
    return fn(count, price_cents, fee_multiplier=m)


def fee_breakdown(
    count: int,
    price_cents: int,
    *,
    fee_type: str = "taker",
    fee_multiplier: int | str | Fraction = 1,
) -> dict[str, object]:
    """Ambos modelos lado a lado, en strings USD de 4 decimales (sin floats)."""
    exact_cc = fee_centicents_exact(
        count, price_cents, fee_type=fee_type, fee_multiplier=fee_multiplier
    )
    conservative_c = fee_cents_conservative(
        count, price_cents, fee_type=fee_type, fee_multiplier=fee_multiplier
    )
    conservative_cc = conservative_c * 100
    return {
        "fee_type": fee_type,
        "fee_multiplier": str(_multiplier(fee_multiplier)),
        "count": count,
        "price_cents": price_cents,
        "exact_model": FEE_MODEL_EXACT,
        "exact_fee_usd": usd4(exact_cc),
        "conservative_model": FEE_MODEL_CONSERVATIVE,
        "conservative_fee_usd": usd4(conservative_cc),
        "overestimate_usd": usd4(conservative_cc - exact_cc),
    }


def usd4(centicents: int) -> str:
    sign = "-" if centicents < 0 else ""
    whole, frac = divmod(abs(centicents), 10_000)
    return f"{sign}{whole}.{frac:04d}"


def parse_usd_to_centicents(value: object) -> int | None:
    """'0.0175' → 175. None si no es un decimal exacto representable en centicents."""
    if not isinstance(value, str) or not value or len(value) > 32:
        return None
    try:
        frac = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    cc = frac * 10_000
    if cc.denominator != 1:
        return None
    return int(cc)
