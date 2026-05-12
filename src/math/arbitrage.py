"""Detección de oportunidades de arbitraje en Kalshi."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.math.fees import kalshi_fee_cents


@dataclass(frozen=True, slots=True)
class ArbLeg:
    """Una pata de un arbitraje (qué comprar, dónde, cuánto)."""

    market_ticker: str
    side: Literal["yes", "no"]
    price_cents: int
    count: int
    available_size: int


@dataclass(frozen=True, slots=True)
class ArbOpportunity:
    """Una oportunidad de arbitraje detectada."""

    legs: tuple[ArbLeg, ...]
    count: int
    gross_profit_cents: int
    fees_cents: int
    net_profit_cents: int
    edge_pct: float

    @property
    def total_cost_cents(self) -> int:
        return sum(leg.price_cents * leg.count for leg in self.legs)


def detect_binary_arb(
    market_ticker: str,
    yes_ask_cents: int,
    yes_available_size: int,
    no_ask_cents: int,
    no_available_size: int,
    *,
    max_count: int = 100,
    min_edge_pct: float = 0.0,
) -> ArbOpportunity | None:
    """
    Detecta arbitraje binario YES+NO sobre el MISMO ticker.

    Compra YES y NO simultáneamente: uno siempre paga 100¢, garantizando
    ganancia si yes_ask + no_ask < 100 y los fees no consumen el spread.

    Args:
        market_ticker: Identificador del market en Kalshi.
        yes_ask_cents: Precio ask del lado YES (1-99 inclusive).
        yes_available_size: Liquidez disponible en YES a ese precio.
        no_ask_cents: Precio ask del lado NO (1-99 inclusive).
        no_available_size: Liquidez disponible en NO a ese precio.
        max_count: Máximo de contratos a tomar por pata. Default 100.
        min_edge_pct: Edge mínimo requerido en %. Default 0.0.

    Returns:
        ArbOpportunity si hay arbitraje viable, None si no.

    Raises:
        ValueError: Si precios fuera de [1, 99], sizes < 0,
                    max_count < 1, o min_edge_pct < 0.

    Examples:
        >>> detect_binary_arb("TICKER", 40, 200, 45, 200) is not None
        True
        >>> detect_binary_arb("TICKER", 50, 100, 55, 100) is None
        True
    """
    if not (1 <= yes_ask_cents <= 99):
        raise ValueError(f"yes_ask_cents debe estar en [1, 99], got {yes_ask_cents}")
    if not (1 <= no_ask_cents <= 99):
        raise ValueError(f"no_ask_cents debe estar en [1, 99], got {no_ask_cents}")
    if yes_available_size < 0:
        raise ValueError(f"yes_available_size debe ser >= 0, got {yes_available_size}")
    if no_available_size < 0:
        raise ValueError(f"no_available_size debe ser >= 0, got {no_available_size}")
    if max_count < 1:
        raise ValueError(f"max_count debe ser >= 1, got {max_count}")
    if min_edge_pct < 0:
        raise ValueError(f"min_edge_pct debe ser >= 0, got {min_edge_pct}")

    if yes_ask_cents + no_ask_cents >= 100:
        return None

    count = min(yes_available_size, no_available_size, max_count)
    if count <= 0:
        return None

    gross_profit = (100 - yes_ask_cents - no_ask_cents) * count
    fees = kalshi_fee_cents(count, yes_ask_cents) + kalshi_fee_cents(count, no_ask_cents)
    net_profit = gross_profit - fees

    if net_profit <= 0:
        return None

    total_cost = (yes_ask_cents + no_ask_cents) * count
    edge_pct = net_profit / total_cost * 100

    if edge_pct < min_edge_pct:
        return None

    yes_leg = ArbLeg(
        market_ticker=market_ticker,
        side="yes",
        price_cents=yes_ask_cents,
        count=count,
        available_size=yes_available_size,
    )
    no_leg = ArbLeg(
        market_ticker=market_ticker,
        side="no",
        price_cents=no_ask_cents,
        count=count,
        available_size=no_available_size,
    )

    return ArbOpportunity(
        legs=(yes_leg, no_leg),
        count=count,
        gross_profit_cents=gross_profit,
        fees_cents=fees,
        net_profit_cents=net_profit,
        edge_pct=edge_pct,
    )


def detect_multi_outcome_arb(
    event_ticker: str,  # noqa: ARG001 — para contexto/logging del caller
    legs: list[tuple[str, int, int]],
    *,
    max_count: int = 100,
    min_edge_pct: float = 0.0,
) -> ArbOpportunity | None:
    """
    Detecta arbitraje en evento con N markets mutuamente excluyentes.

    Compra YES en cada outcome: exactamente uno pagará 100¢, garantizando
    ganancia si sum(ask_cents) < 100 y los fees no consumen el spread.

    Args:
        event_ticker: Identificador del evento (para contexto del caller).
        legs: Lista de (market_ticker, ask_cents, available_size). Mínimo 2.
        max_count: Máximo de contratos a tomar por pata. Default 100.
        min_edge_pct: Edge mínimo requerido en %. Default 0.0.

    Returns:
        ArbOpportunity si hay arbitraje viable, None si no.

    Raises:
        ValueError: Si len(legs) < 2, precios fuera de [1, 99], o sizes < 0.

    Examples:
        >>> legs = [("A", 30, 100), ("B", 30, 100), ("C", 30, 100)]
        >>> detect_multi_outcome_arb("EVENT", legs) is not None
        True
        >>> detect_multi_outcome_arb("EVENT", [("A", 35, 100), ("B", 35, 100), ("C", 35, 100)]) is None
        True
    """
    if len(legs) < 2:
        raise ValueError(f"Se requieren al menos 2 legs, got {len(legs)}")
    if max_count < 1:
        raise ValueError(f"max_count debe ser >= 1, got {max_count}")
    if min_edge_pct < 0:
        raise ValueError(f"min_edge_pct debe ser >= 0, got {min_edge_pct}")

    for ticker, ask, size in legs:
        if not (1 <= ask <= 99):
            raise ValueError(f"ask_cents debe estar en [1, 99] para {ticker}, got {ask}")
        if size < 0:
            raise ValueError(f"available_size debe ser >= 0 para {ticker}, got {size}")

    sum_asks = sum(ask for _, ask, _ in legs)
    if sum_asks >= 100:
        return None

    min_size = min(size for _, _, size in legs)
    count = min(min_size, max_count)
    if count <= 0:
        return None

    gross_profit = (100 - sum_asks) * count
    fees = sum(kalshi_fee_cents(count, ask) for _, ask, _ in legs)
    net_profit = gross_profit - fees

    if net_profit <= 0:
        return None

    total_cost = sum_asks * count
    edge_pct = net_profit / total_cost * 100

    if edge_pct < min_edge_pct:
        return None

    arb_legs = tuple(
        ArbLeg(
            market_ticker=ticker,
            side="yes",
            price_cents=ask,
            count=count,
            available_size=size,
        )
        for ticker, ask, size in legs
    )

    return ArbOpportunity(
        legs=arb_legs,
        count=count,
        gross_profit_cents=gross_profit,
        fees_cents=fees,
        net_profit_cents=net_profit,
        edge_pct=edge_pct,
    )
