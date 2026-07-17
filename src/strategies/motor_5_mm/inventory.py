"""
Inventario simulado del Motor 5 (F1) — posición neta por ticker + PnL mark-to-market.

Convención (todo en el eje YES, como cotiza el book V2):
  - net_contracts > 0 = largos YES (compramos con nuestro bid).
  - net_contracts < 0 = cortos YES (vendimos con nuestro ask; económicamente = largos NO).
  - cash_cents: flujo de caja acumulado (compra resta precio·count, venta lo suma).
  - fees_cents: comisión REAL de cada fill (kalshi_fee_cents con el count del fill —
    anti-patrón: fee por contrato suelto; la fórmula NO es lineal en count).

PnL mark-to-market = cash − fees + net·mark, con mark = mid del book (o el fair·100 si
no hay book). Es el número del gate F1→F2: "PnL shadow neto de fees positivo y estable".

Regla de oro (Lección 9, plan §2): este estado es una state machine mutable — la aplica
SOLO el engine, secuencialmente, y cualquier excepción al aplicarla marca el ticker como
corrupto aguas arriba (el engine descarta la quote viva y re-sincroniza).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.math.fees import kalshi_fee_cents
from src.strategies.motor_5_mm.shadow_fill import ShadowFill


@dataclass(slots=True)
class TickerInventory:
    net_contracts: int = 0
    cash_cents: int = 0
    fees_cents: int = 0
    fills: int = 0

    def mtm_cents(self, mark_cents: float) -> int:
        """PnL si liquidáramos el neto al mark AHORA (sin fee de salida — se paga al salir;
        el sesgo es ≤2¢/contrato y explícito aquí)."""
        return int(round(self.cash_cents - self.fees_cents + self.net_contracts * mark_cents))


class InventoryBook:
    """Inventario simulado de TODOS los tickers cotizados. Instancia del engine (no global)."""

    def __init__(self) -> None:
        self.positions: dict[str, TickerInventory] = {}

    def apply_fill(self, fill: ShadowFill) -> TickerInventory:
        inv = self.positions.setdefault(fill.ticker, TickerInventory())
        fee = kalshi_fee_cents(fill.count, fill.price_cents)
        if fill.side == "buy":
            inv.net_contracts += fill.count
            inv.cash_cents -= fill.price_cents * fill.count
        elif fill.side == "sell":
            inv.net_contracts -= fill.count
            inv.cash_cents += fill.price_cents * fill.count
        else:  # pragma: no cover - side viene de ShadowFill, cerrado
            raise ValueError(f"side inválido: {fill.side!r}")
        inv.fees_cents += fee
        inv.fills += 1
        return inv

    def net(self, ticker: str) -> int:
        inv = self.positions.get(ticker)
        return inv.net_contracts if inv else 0

    def total_abs_contracts(self) -> int:
        return sum(abs(inv.net_contracts) for inv in self.positions.values())

    def total_mtm_cents(self, marks: dict[str, float]) -> int:
        """PnL total con marks por ticker. Ticker sin mark este tick (book caído y fair
        vencido) se marca al prior NEUTRAL 50¢ — marcar a 0 sobreestimaría los cortos y
        subestimaría los largos; 50 es el punto de máxima incertidumbre de un binario."""
        return sum(inv.mtm_cents(marks.get(t, 50.0)) for t, inv in self.positions.items())
