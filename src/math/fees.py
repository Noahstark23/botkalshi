"""Fee de Kalshi para un trade."""

from __future__ import annotations


def kalshi_fee_cents(count: int, price_cents: int) -> int:
    """
    Fee de Kalshi para un trade, en centavos.

    Fórmula oficial: ceil(0.07 * count * price_cents * (100 - price_cents) / 10000)

    La implementación usa aritmética entera equivalente con denominador 1_000_000
    para evitar bugs de float precision en boundary cases:
        numerator = 7 * count * price_cents * (100 - price_cents)
        return (numerator + 999_999) // 1_000_000

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
        >>> kalshi_fee_cents(100, 50)
        2
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
    return (numerator + 999_999) // 1_000_000
