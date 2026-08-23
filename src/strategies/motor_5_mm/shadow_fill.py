"""
Shadow fills del Motor 5 — PURO: quote resting del tick anterior + book actual → fills.

REGLA CONSERVADORA (plan F1 §alcance 3 — la fuente #1 de sobre-optimismo en backtests
de MM es asumir fills que no habrían ocurrido):

  Un fill hipotético existe SOLO si el mercado CRUZA ESTRICTAMENTE el precio de la quote
  entre un tick y el siguiente:
    - Nuestro BID resting a b¢ se llena solo si el mejor ASK del book baja POR DEBAJO
      de b (best_yes_ask < b): alguien ofreció vender más barato que nuestro precio →
      nos habría llenado a b. Que el ask TOQUE b (==) NO llena: la prioridad de cola
      contra las órdenes reales a ese precio es incognoscible desde afuera.
    - Simétrico para el ASK resting a a¢: solo si best_yes_bid > a.

  Limitaciones aceptadas (documentadas para la revisión adversarial del gate F1→F2):
    - Resolución = cadence del loop (~60s): cruces intra-tick que revierten no se ven
      (sesgo CONSERVADOR: perdemos fills reales, nunca inventamos).
    - Máximo un fill por lado por tick, por el size completo de la quote (sin parciales).
    - Sin feed de `trade` prints en F1; el cruce se infiere del top-of-book.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.strategies.motor_5_mm.quoter import QuoteSet


@dataclass(frozen=True, slots=True)
class ShadowFill:
    ticker: str
    side: str  # "buy" (nuestro bid llenado) | "sell" (nuestro ask llenado)
    price_cents: int  # el precio de NUESTRA quote (a eso nos habrían llenado)
    count: int
    rule: str  # evidencia del cruce, p.ej. "ask 38 < bid 40"
    observed_depth: Decimal | None = None  # size fixed-point del BBO que cruzó la quote


def fills_for_quote(
    quote: QuoteSet,
    *,
    best_yes_bid: int | None,
    best_yes_ask: int | None,
    best_yes_bid_depth: Decimal | None = None,
    best_yes_ask_depth: Decimal | None = None,
    observable_count_cap: int | None = None,
) -> list[ShadowFill]:
    """Fills hipotéticos de UNA quote resting contra el top-of-book ACTUAL.

    Un cruce del top prueba que al menos un contrato pudo llenar, no que había volumen
    suficiente para llenar toda la quote. Cuando se pasa ``observable_count_cap`` el
    cruce solo cuenta si el size fixed-point de la punta cruzada cubre el ``count`` que
    afirmamos. F1-v2 pasa cap=1: 0.33 contratos visibles no pueden convertirse en un fill
    de un contrato entero.
    """
    count = quote.size
    if observable_count_cap is not None:
        if observable_count_cap < 1:
            raise ValueError("observable_count_cap debe ser >= 1")
        count = min(count, observable_count_cap)
    fills: list[ShadowFill] = []
    buy_crossed = (
        quote.bid_cents is not None and best_yes_ask is not None and best_yes_ask < quote.bid_cents
    )
    buy_depth_covers = observable_count_cap is None or (
        best_yes_ask_depth is not None and best_yes_ask_depth >= Decimal(count)
    )
    if buy_crossed and buy_depth_covers:
        fills.append(
            ShadowFill(
                ticker=quote.ticker,
                side="buy",
                price_cents=quote.bid_cents,
                count=count,
                rule=f"ask {best_yes_ask} < bid {quote.bid_cents}",
                observed_depth=best_yes_ask_depth,
            )
        )
    sell_crossed = (
        quote.ask_cents is not None and best_yes_bid is not None and best_yes_bid > quote.ask_cents
    )
    sell_depth_covers = observable_count_cap is None or (
        best_yes_bid_depth is not None and best_yes_bid_depth >= Decimal(count)
    )
    if sell_crossed and sell_depth_covers:
        fills.append(
            ShadowFill(
                ticker=quote.ticker,
                side="sell",
                price_cents=quote.ask_cents,
                count=count,
                rule=f"bid {best_yes_bid} > ask {quote.ask_cents}",
                observed_depth=best_yes_bid_depth,
            )
        )
    return fills
