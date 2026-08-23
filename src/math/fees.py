"""Fee de Kalshi para un trade."""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction


def _multiplier(value: int | float | str | Fraction) -> Fraction:
    """Normaliza M sin introducir error binario antes del ceil por orden."""
    try:
        result = value if isinstance(value, Fraction) else Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"fee_multiplier inválido: {value!r}") from exc
    if result < 0:
        raise ValueError(f"fee_multiplier debe ser >= 0, got {value!r}")
    return result


def maker_fee_multiplier_for_ticker(ticker: str, *, as_of: datetime | None = None) -> Fraction:
    """Multiplicador maker conocido para la serie, con fallback conservador M=1.

    Kalshi cambió KXMLBGAME a M=0.5 el 2026-08-07T04:59:45.131Z. La fecha se
    persiste indirectamente junto al fill y el multiplicador efectivo se guarda en
    ``mm_shadow_fills``: una modificación futura del schedule no reescribe el pasado.

    Las series sin un cambio verificado conservan M=1. Es deliberadamente conservador
    para el shadow y, sobre todo, evita aplicar el 0.5 de MLB a NFL u otra serie.
    """
    series = ticker.split("-", 1)[0].upper()
    when = as_of or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if series == "KXMLBGAME" and when >= datetime(2026, 8, 7, 4, 59, 45, 131000, tzinfo=UTC):
        return Fraction(1, 2)
    return Fraction(1, 1)


def kalshi_fee_cents(
    count: int,
    price_cents: int,
    *,
    fee_multiplier: int | float | str | Fraction = 1,
) -> int:
    """
    Fee de Kalshi para un trade, en centavos.

    Fórmula oficial (fee schedule de Kalshi): fee = round-up-al-centavo de
    0.07 × count × P × (1 − P), con P en dólares. En centavos enteros eso es:
        fee_cents = ceil(7 * count * price_cents * (100 - price_cents) / 10_000)

    La implementación usa aritmética entera equivalente para evitar bugs de
    float precision en boundary cases:
        numerator = 7 * count * price_cents * (100 - price_cents)
        return (numerator + 9_999) // 10_000

    NOTA (fix auditoría 2026-07-01): la versión anterior dividía por 1_000_000,
    lo que produce el fee en DÓLARES ceileados pero etiquetado como centavos
    (~100× subestimado; fee(100, 50) daba 2 en vez de 175). El fósil venía de
    KALSHI_BOT_CONTEXT.md §6, también corregido.

    Casos especiales:
        - count == 0  → 0 (no hay trade)
        - price_cents == 0   → 0 (math da 0)
        - price_cents == 100 → 0 (math da 0)

    Args:
        count: Número de contratos. Debe ser >= 0.
        price_cents: Precio del contrato en centavos (0-100 inclusive).

    Returns:
        Fee en centavos (entero, redondeado hacia arriba).

    Raises:
        ValueError: Si count < 0, price_cents < 0, o price_cents > 100.

    Examples:
        >>> kalshi_fee_cents(100, 50)   # ejemplo oficial: $1.75
        175
        >>> kalshi_fee_cents(1, 95)     # ejemplo oficial: 0.3325¢ → 1¢
        1
        >>> kalshi_fee_cents(0, 50)
        0
        >>> kalshi_fee_cents(100, 0)
        0
        >>> kalshi_fee_cents(100, 100)
        0
    """
    if count < 0:
        raise ValueError(f"count debe ser >= 0, got {count}")
    if price_cents < 0:
        raise ValueError(f"price_cents debe ser >= 0, got {price_cents}")
    if price_cents > 100:
        raise ValueError(f"price_cents debe ser <= 100, got {price_cents}")

    if count == 0 or price_cents == 0 or price_cents == 100:
        return 0

    multiplier = _multiplier(fee_multiplier)
    numerator = 7 * count * price_cents * (100 - price_cents) * multiplier.numerator
    denominator = 10_000 * multiplier.denominator
    return (numerator + denominator - 1) // denominator


def kalshi_maker_fee_cents(
    count: int,
    price_cents: int,
    *,
    fee_multiplier: int | float | str | Fraction = 1,
) -> int:
    """
    Fee de MAKER de Kalshi para un fill de orden resting, en centavos.

    Fórmula oficial del schedule maker:
        maker fee = round up(M × 0.0175 × C × P × (1−P))
    — ¼ de la tasa de taker (0.07). M es POR SERIE y puede cambiar: KXMLBGAME pasó
    a M=0.5 el 2026-08-07, mientras NFL/NBA/EPL seguían en M=1 al verificarse el
    2026-08-22. El hot path obtiene y valida M con GET /series; esta función solo
    ejecuta la aritmética exacta del multiplicador que recibe.

    Se cobra AL EJECUTAR la orden resting (cancelar es gratis). Mismo ceil por
    orden que la taker: medirla a count=1 sobreestima por contrato.

    Aritmética entera equivalente (0.0175 = 7/400):
        fee_cents = ceil(7 * count * price_cents * (100 - price_cents) / 40_000)

    Examples:
        >>> kalshi_maker_fee_cents(100, 50)   # ¼ del ejemplo oficial de taker
        44
        >>> kalshi_maker_fee_cents(10, 50)    # 4.375¢ → 5¢ (ceil por orden)
        5
        >>> kalshi_maker_fee_cents(0, 50)
        0
    """
    if count < 0:
        raise ValueError(f"count debe ser >= 0, got {count}")
    if price_cents < 0:
        raise ValueError(f"price_cents debe ser >= 0, got {price_cents}")
    if price_cents > 100:
        raise ValueError(f"price_cents debe ser <= 100, got {price_cents}")

    if count == 0 or price_cents == 0 or price_cents == 100:
        return 0

    multiplier = _multiplier(fee_multiplier)
    numerator = 7 * count * price_cents * (100 - price_cents) * multiplier.numerator
    denominator = 40_000 * multiplier.denominator
    return (numerator + denominator - 1) // denominator
