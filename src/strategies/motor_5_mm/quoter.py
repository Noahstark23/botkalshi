"""
Quoter del Motor 5 — PURO: fair + book + inventario → QuoteSet o (None, motivo).

Reglas (plan F1 §alcance 2, decisiones documentadas):
  1. Centro = fair − skew. El skew desplaza AMBAS quotes contra el inventario:
     largo (net>0) → centro más bajo (vende más fácil, compra menos) y viceversa.
     skew = (inventario / max_inventario) · half_spread, saturado a ±half_spread.
  1b. EDGE-SKEW (asymmetric quoting, propuesta 2026-07-13, F2): si el mid del BOOK está
     desplazado del fair, el centro se inclina edge_skew_cents HACIA el fair-side barato
     (book bajo el fair → centro más alto → bid más agresivo: capturar más flujo del lado
     donde el mercado está equivocado). 0 = off (comportamiento F1 exacto). El post-only
     y el spread-mínimo-rentable (reglas 3 y 5) siguen mandando DESPUÉS del lean.
  2. bid = floor(centro − half_spread), ask = ceil(centro + half_spread) — el redondeo
     siempre ENSANCHA (nunca cotiza más agresivo que el spread configurado).
  3. Emulación post-only (fallback F3 anticipado): la quote NUNCA cruza el book —
     bid ≤ best_ask − 1 y ask ≥ best_bid + 1. Una quote que cruzara sería taker, no maker.
  4. Clamp [1, 99]. Si tras clamps bid ≥ ask → quote degenerada → no se cotiza.
  5. Spread mínimo RENTABLE con la fee real (kalshi_fee_cents exacta, anti-patrón 7% flat):
     el round-trip bilateral captura (ask − bid)¢/contrato y paga fee(bid) + fee(ask);
     si no queda margen positivo → no se cotiza (skip_unprofitable).
  6. Tope de inventario: |net| ≥ max → se cotiza SOLO el lado que REDUCE (one-sided).
     Una quote one-sided no exige la regla 5 (reduce riesgo, no captura spread).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from src.math.fees import (
    kalshi_fee_cents,
    kalshi_maker_fee_cents,
)


@dataclass(frozen=True, slots=True)
class QuoteSet:
    """Quote bilateral (o one-sided en tope de inventario) de UN market. Precios YES."""

    ticker: str
    fair_prob: float
    bid_cents: int | None  # None = lado retirado (tope de inventario largo)
    ask_cents: int | None  # None = lado retirado (tope de inventario corto)
    size: int  # contratos por lado


def compute_quote(
    ticker: str,
    fair_prob: float,
    *,
    half_spread_cents: int,
    size_contracts: int,
    inventory_contracts: int,
    max_inventory_contracts: int,
    best_yes_bid: int | None,
    best_yes_ask: int | None,
    edge_skew_cents: int = 0,
    fees_as_maker: bool = False,
    fee_multiplier: int | float | str = 1,
) -> tuple[QuoteSet | None, str | None]:
    """(QuoteSet, None) si hay quote emitible; (None, motivo_de_skip) si no."""
    if not (0.0 < fair_prob < 1.0):
        return None, "fair_out_of_range"
    fair_cents = fair_prob * 100.0
    ratio = max(-1.0, min(1.0, inventory_contracts / max_inventory_contracts))
    center = fair_cents - ratio * half_spread_cents
    # 1b. Edge-skew: lean hacia el lado donde el book está equivocado vs el fair. Solo con
    # book bilateral (sin mid no hay dirección) y saturado a ±edge_skew_cents.
    if edge_skew_cents > 0 and best_yes_bid is not None and best_yes_ask is not None:
        book_mid = (best_yes_bid + best_yes_ask) / 2.0
        if fair_cents > book_mid:
            center += edge_skew_cents  # mercado subvaluado → comprar más agresivo
        elif fair_cents < book_mid:
            center -= edge_skew_cents  # mercado sobrevaluado → vender más agresivo
    bid: int | None = math.floor(center - half_spread_cents)
    ask: int | None = math.ceil(center + half_spread_cents)
    # Post-only emulado: nunca cruzar (ni tocar) el mejor precio contrario del book.
    if best_yes_ask is not None:
        bid = min(bid, best_yes_ask - 1)
    if best_yes_bid is not None:
        ask = max(ask, best_yes_bid + 1)
    bid = max(1, min(99, bid))
    ask = max(1, min(99, ask))
    if bid >= ask:
        return None, "degenerate"
    # Tope de inventario → one-sided (solo el lado que reduce |net|).
    if inventory_contracts >= max_inventory_contracts:
        bid = None
    elif inventory_contracts <= -max_inventory_contracts:
        ask = None
    if bid is not None and ask is not None:
        captured = ask - bid
        if fees_as_maker:
            # APUESTA 1, corregida 2026-08-13 contra el PDF oficial (July 7, 2026): el
            # maker NO paga $0 — usa ¼ del taker (0.0175) × multiplicador de la serie.
            # La regla se evalúa AL SIZE de la quote: el ceil es POR ORDEN y medirlo a
            # count=1 sobreestima por contrato (a size=10 y 50¢: 0.5¢/contrato/pata
            # real vs 1¢ medido a count=1). Round-trip maker en zona media ≈ 1¢/contrato
            # → spreads capturados ≥2¢ son rentables; el modelo taker exigía ≥4¢.
            fees_orden = kalshi_maker_fee_cents(
                size_contracts, bid, fee_multiplier=fee_multiplier
            ) + kalshi_maker_fee_cents(size_contracts, ask, fee_multiplier=fee_multiplier)
            if captured * size_contracts <= fees_orden:
                return None, "unprofitable"
        else:
            # Modelo taker legacy (el "fee fantasma" para un flujo post_only): intacto
            # como default hasta que el operador encienda el flag.
            fees = kalshi_fee_cents(1, bid, fee_multiplier=fee_multiplier) + kalshi_fee_cents(
                1, ask, fee_multiplier=fee_multiplier
            )
            if captured <= fees:
                return None, "unprofitable"
    return QuoteSet(
        ticker=ticker, fair_prob=fair_prob, bid_cents=bid, ask_cents=ask, size=size_contracts
    ), None
