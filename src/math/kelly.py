"""Sizing fractional Kelly para Kalshi binary contracts."""

from __future__ import annotations

import math


def quarter_kelly_size(
    true_prob: float,
    price_cents: int,
    bankroll_cents: int,
    *,
    kelly_fraction: float = 0.25,
    max_pct: float = 5.0,
) -> int:
    """
    Sizing fractional Kelly con cap absoluto.

    Para Kalshi binary contracts ($1 payout):
        full_kelly = (true_prob - implied_prob) / (1 - implied_prob)
        donde implied_prob = price_cents / 100

    Tamaño = floor(min(kelly_fraction * full_kelly, max_pct / 100) * bankroll / price_cents)

    Args:
        true_prob: Probabilidad estimada de YES (0.0 a 1.0, exclusivos).
        price_cents: Precio ask en cents (1-99 inclusive).
        bankroll_cents: Capital activo en cents.
        kelly_fraction: Fracción de Kelly. Default 0.25 (¼ Kelly).
        max_pct: Cap absoluto en % de bankroll. Default 5.0.

    Returns:
        Número de contratos a comprar. 0 si no hay edge o bankroll insuficiente.

    Raises:
        ValueError: Si algún parámetro está fuera de su rango válido.

    Note:
        Este Kelly es para Motor 2 (sportsbook consensus) y Motor 3 (CLV),
        donde existe estimación de probabilidad. NO se usa en Motor 1 (arbitraje),
        donde sizing se basa en liquidez disponible, no en Kelly.

    Examples:
        >>> quarter_kelly_size(0.60, 50, 30000)
        30
        >>> quarter_kelly_size(0.90, 50, 30000)
        30
        >>> quarter_kelly_size(0.50, 50, 30000)
        0
    """
    if not (0.0 < true_prob < 1.0):
        raise ValueError(f"true_prob debe estar en (0, 1), got {true_prob}")
    if not (0 < price_cents < 100):
        raise ValueError(f"price_cents debe estar en (1, 99), got {price_cents}")
    if bankroll_cents < 0:
        raise ValueError(f"bankroll_cents debe ser >= 0, got {bankroll_cents}")
    if not (0.0 < kelly_fraction <= 1.0):
        raise ValueError(f"kelly_fraction debe estar en (0, 1], got {kelly_fraction}")
    if not (0.0 < max_pct <= 100.0):
        raise ValueError(f"max_pct debe estar en (0, 100], got {max_pct}")

    if bankroll_cents == 0:
        return 0

    implied_prob = price_cents / 100.0
    full_kelly = (true_prob - implied_prob) / (1.0 - implied_prob)

    if full_kelly <= 0:
        return 0

    fraction = min(kelly_fraction * full_kelly, max_pct / 100.0)
    # round before floor: avoids float precision loss when true value is exactly N.0
    # (e.g. 0.60 - 0.50 = 0.09999... in float makes fraction land just below cap)
    return math.floor(round(fraction * bankroll_cents / price_cents, 10))
