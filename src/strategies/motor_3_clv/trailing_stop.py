"""
Trailing-stop detector (Motor 3, FASE 2) — salida por RETROCESO del bid desde su máximo.

Evolución del take-profit fijo: en vez de asegurar a un umbral duro, deja correr la ganancia y
cierra solo cuando el bid retrocede `drop_cents` desde el pico observado (`peak_bid`). Para no
convertir esto en un stop-loss encubierto, **solo se arma como protección de GANANCIA**: el pico
tiene que superar el entry POR AL MENOS `drop_cents` (fix auditoría 2026-07-01 — con solo
`peak > entry`, un pico de entry+1 permitía vender hasta entry−(drop−1)). Así el precio de
disparo (peak − drop) nunca queda debajo del entry. Las pérdidas las maneja el RiskManager.

Funciones puras (espejo de take_profit.py): reciben el bid live y el entry YA resueltos (el engine
los trae del orderbook y de la pata BUY), no tocan red ni DB → testeables sin mocks. El `peak_bid`
lo persiste el engine en `PortfolioPosition.peak_bid_cents` entre ticks.
"""

from __future__ import annotations

from src.math.fees import kalshi_fee_cents
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
    - peak_bid − entry_bid < drop_cents → False (sin margen suficiente: el disparo quedaría
      debajo del entry → sería stop-loss encubierto, no trailing).
    - current_bid > peak_bid → False (defensa: el pico es el máximo; si current lo supera, el
      caller no actualizó el peak — no disparar con datos incoherentes).
    - vender al bid ACTUAL no deja ganancia neta de fees → False (auditoría rentabilidad
      2026-07-07: el armado garantiza que el DISPARO (peak−drop) no quede bajo el entry,
      pero el FILL es al bid real — en el borde exacto (bid == entry) vendía al entry
      realizando −2 fees seguros, y en un GAP (bid muy por debajo del peak) vendía DEBAJO
      del entry: el stop-loss encubierto que este módulo promete no ser. Mismo gate net>0
      que ya usa el take-profit).
    - current_bid <= peak_bid - drop_cents → True (retroceso suficiente → asegurar).
    """
    if drop_cents <= 0:
        return False
    if position.count <= 0:
        return False
    if peak_bid is None or current_bid is None or entry_bid is None:
        return False
    if peak_bid - entry_bid < drop_cents:
        return False
    if current_bid > peak_bid:
        return False
    net_per_contract = (
        current_bid - entry_bid - kalshi_fee_cents(1, current_bid) - kalshi_fee_cents(1, entry_bid)
    )
    if net_per_contract <= 0:
        return False
    return current_bid <= peak_bid - drop_cents
