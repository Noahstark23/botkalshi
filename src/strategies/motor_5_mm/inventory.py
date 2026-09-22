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

from src.math.fees import (
    kalshi_fee_cents,
    kalshi_maker_fee_cents,
)
from src.strategies.motor_5_mm.shadow_fill import ShadowFill


def validar_fee_registrada(valor: object) -> int:
    """Una comisión GRABADA en `mm_shadow_fills` es válida solo si es un entero ≥ 0.

    ALCANCE: este es el contrato del esquema LEGACY de esta tabla (`fee_effective_cents`
    INTEGER, en centavos). NO es el formato general de Kalshi, que publica comisiones en
    dólares con precisión de seis decimales; un adaptador futuro para ese formato se
    versiona aparte, sin truncar a centavos.

    Reproducir un dato histórico no es aceptar cualquier valor: la comisión neta de Kalshi
    es no negativa, y SQLite tiene tipado dinámico — una columna INTEGER guarda un REAL o
    un TEXT sin quejarse y el driver los devuelve con su tipo nativo. El cero documentado
    es válido; un dato ausente (None) es otro hecho y lo resuelve quien llama.

    ⚠️ `bool` se excluye ANTES del chequeo de `int`: en Python `isinstance(True, int)` es
    True, así que sin esto `True` entraría como 1¢ sin que nadie lo note.
    """
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ValueError(
            f"comisión registrada fuera de contrato: tipo {type(valor).__name__} "
            "(se espera un entero ≥ 0 en centavos)"
        )
    if valor < 0:
        raise ValueError(
            f"comisión registrada fuera de contrato: {valor}¢ es negativa "
            "(la comisión neta de Kalshi es no negativa)"
        )
    return valor


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
    """Inventario simulado de TODOS los tickers cotizados. Instancia del engine (no global).

    fees_as_maker (APUESTA 1, corregida 2026-08-13 contra el PDF oficial): True = los
    fills shadow pagan la fee REAL de maker — ¼ del taker (0.0175, multiplicador 1 en
    deportes), NO $0 como creía el dossier. El default False conserva el modelo taker
    histórico, que para un flujo post_only cobraba 4× de más (el "fee fantasma")."""

    def __init__(self, *, fees_as_maker: bool = False) -> None:
        self.positions: dict[str, TickerInventory] = {}
        self._fees_as_maker = fees_as_maker

    def apply_fill(
        self,
        fill: ShadowFill,
        *,
        fee_multiplier: int | float | str = 1,
        fee_cents_recorded: int | None = None,
    ) -> TickerInventory:
        """Aplica un fill. `fee_cents_recorded` REPRODUCE una comisión ya cobrada.

        La recuperación de una cohorte (engine._rehidratar_inventory) pasa el
        `fee_effective_cents` de la fila: el hecho histórico manda sobre cualquier
        recálculo. Recalcular en cada arranque hace que la comisión dependa del modo
        VIVO del engine (`_fees_as_maker`) y del multiplicador vigente, así que una
        cohorte grabada como taker rehidratada en modo maker cambiaba de caja al
        reiniciar — medido: 2¢ reales reconstruidos como 1¢. Y la tarifa se mueve: la
        API de la serie KXMLBGAME publica hoy `fee_multiplier: 0.5`, pero eso no dice qué
        regía en cada fecha pasada, y Kalshi documenta overrides por EVENTO. La tarifa de
        hoy no puede reescribir el pasado.

        Reproducir fielmente NO es aceptar cualquier valor: se valida contra el contrato
        (entero ≥ 0) ANTES de tocar `positions` — el `setdefault` va después a propósito,
        para que un rechazo no deje una entrada vacía de un ticker que nunca operó.
        """
        if fee_cents_recorded is not None:
            fee = validar_fee_registrada(fee_cents_recorded)
        else:
            fee = (
                kalshi_maker_fee_cents(fill.count, fill.price_cents, fee_multiplier=fee_multiplier)
                if self._fees_as_maker
                else kalshi_fee_cents(fill.count, fill.price_cents, fee_multiplier=fee_multiplier)
            )
        inv = self.positions.setdefault(fill.ticker, TickerInventory())
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
