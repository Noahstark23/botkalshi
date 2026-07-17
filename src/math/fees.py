"""Fee de Kalshi para un trade."""

from __future__ import annotations


def kalshi_fee_cents(count: int, price_cents: int) -> int:
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

    numerator = 7 * count * price_cents * (100 - price_cents)
    return (numerator + 9_999) // 10_000
