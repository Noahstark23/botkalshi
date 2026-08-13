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

from src.math.fees import kalshi_fee_cents


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
        # APUESTA 1 (2026-08-12): con fees_as_maker el round-trip post_only no paga
        # comisión (maker fee $0 en el schedule de deportes de Kalshi) — la regla 5
        # exige solo spread positivo. El modelo taker (default) era el "fee fantasma"
        # que auto-excluía a M5 de todo book con spread ≤4¢ en zona media.
        fees = 0 if fees_as_maker else kalshi_fee_cents(1, bid) + kalshi_fee_cents(1, ask)
        if captured <= fees:
            return None, "unprofitable"
    return QuoteSet(
        ticker=ticker, fair_prob=fair_prob, bid_cents=bid, ask_cents=ask, size=size_contracts
    ), None
