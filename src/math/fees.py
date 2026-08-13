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


def kalshi_maker_fee_cents(count: int, price_cents: int) -> int:
    """
    Fee de MAKER de Kalshi para un fill de orden resting, en centavos.

    Fórmula oficial (fee schedule "Last updated and effective: July 7, 2026",
    verificado 2026-08-13 contra el PDF por el agente web):
        maker fee = round up(M × 0.0175 × C × P × (1−P))
    — ¼ de la tasa de taker (0.07). El multiplicador M de las series DEPORTIVAS
    es 1 (fila literal: "KXMLBGAME | Professional Baseball Game | 1 | 1"); el
    M=0 existe solo en series no deportivas (KXBTCY, KXCPI, KXFED…). La creencia
    "maker $0 en deportes" del dossier del plan era FALSA — murió contra el PDF
    antes de encender ningún flag, que es exactamente para lo que estaba el gate.

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

    numerator = 7 * count * price_cents * (100 - price_cents)
    return (numerator + 39_999) // 40_000
