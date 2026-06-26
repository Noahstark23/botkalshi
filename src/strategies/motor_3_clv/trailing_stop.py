"""
Trailing-stop detector (Motor 3, FASE 2) — salida por RETROCESO del bid desde su máximo.

Evolución del take-profit fijo: en vez de asegurar a un umbral duro, deja correr la ganancia y
cierra solo cuando el bid retrocede `drop_cents` desde el pico observado (`peak_bid`). Para no
convertir esto en un stop-loss encubierto, **solo se arma como protección de GANANCIA**: el pico
tiene que haber superado el entry (`peak_bid > entry_bid`). Las pérdidas las maneja el
RiskManager, no esto.

Funciones puras (espejo de take_profit.py): reciben el bid live y el entry YA resueltos (el engine
los trae del orderbook y de la pata BUY), no tocan red ni DB → testeables sin mocks. El `peak_bid`
lo persiste el engine en `PortfolioPosition.peak_bid_cents` entre ticks.
"""

from __future__ import annotations

from src.storage.models import PortfolioPosition

# Retroceso (cents) desde el pico que dispara el cierre. Placeholder calibrable desde la
# distribución de las líneas [MOTOR 3 TRAIL SHADOW] (Coolify: MOTOR_3_TRAILING_DROP_CENTS).
DEFAULT_TRAILING_DROP_CENTS = 5


def next_peak_bid(peak_bid: int | None, current_bid: int, entry_bid: int) -> int:
    """Nuevo pico del bid. Arranca en el entry (no protegemos por debajo del entry) y solo sube
    con el bid: `max(peak|entry, current)`. Nunca baja (el retroceso lo evalúa trailing_stop_due)."""
    base = entry_bid if peak_bid is None else max(peak_bid, entry_bid)
    return max(base, current_bid)


def trailing_stop_due(
    position: PortfolioPosition,
    peak_bid: int | None,
    current_bid: int | None,
    entry_bid: int | None,
    drop_cents: int = DEFAULT_TRAILING_DROP_CENTS,
) -> bool:
    """True si el bid retrocedió `drop_cents` desde el pico, estando en ganancia.

    - drop_cents <= 0 → False (trailing apagado por config inválida).
    - count <= 0 → False (posición cerrada/inconsistente).
    - peak/current/entry None → False (no decidible: falta el orderbook o la pata BUY).
    - peak_bid <= entry_bid → False (nunca estuvo en ganancia → no es trailing, sería stop-loss).
    - current_bid > peak_bid → False (defensa: el pico es el máximo; si current lo supera, el
      caller no actualizó el peak — no disparar con datos incoherentes).
    - current_bid <= peak_bid - drop_cents → True (retroceso suficiente → asegurar).
    """
    if drop_cents <= 0:
        return False
    if position.count <= 0:
        return False
    if peak_bid is None or current_bid is None or entry_bid is None:
        return False
    if peak_bid <= entry_bid:
        return False
    if current_bid > peak_bid:
        return False
    return current_bid <= peak_bid - drop_cents


def decide_exit(*, take_profit_due: bool, trailing_due: bool, time_due: bool) -> str | None:
    """Precedencia de salida cuando varias condiciones coinciden: take-profit > trailing > tiempo.

    Helper puro/testeable. En el engine el dedup equivalente lo da el dict `exits` (una salida por
    ticker por tick); como exit_position no recibe `reason`, la precedencia solo importa para
    logging/análisis, no cambia QUÉ se vende."""
    if take_profit_due:
        return "take_profit"
    if trailing_due:
        return "trailing_stop"
    if time_due:
        return "time"
    return None
