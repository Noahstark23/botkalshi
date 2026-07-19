"""
Motor 9 "Derrame" — detector PURO de saltos de precio (sin red, sin DB, sin reloj propio).

TESIS (2026-07-18, nace de la auditoría de rentabilidad): en un evento multi-outcome la
probabilidad se conserva — si el outcome A salta +8¢, sus HERMANOS deben ajustar a la baja.
Si ajustan con REZAGO, la ventana entre el salto y el ajuste es capturable comprando el lado
correcto del hermano rezagado. Si el mercado ajusta instantáneo, no hay nada que capturar —
y este detector + su shadow lo van a MEDIR, no suponer.

Este módulo solo detecta el TRIGGER (el salto). La medición del derrame (qué hizo el hermano
DESPUÉS) vive en shadow.py. F1: nada acá importa el cliente de órdenes (test-guard).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

# Tope de puntos (ts, mid) retenidos por ticker además de la poda por tiempo — la lección
# "nada sin tope" (57GB de disco, OOM del bootstrap buffer): un feed hiperactivo no debe
# poder crecer una deque sin límite aunque la ventana temporal sea corta.
_MAX_POINTS_PER_TICKER = 500


@dataclass(frozen=True, slots=True)
class SpilloverTrigger:
    """Un salto detectado: `move_cents` FIRMADO (positivo = el mid subió)."""

    ticker: str
    move_cents: float
    mid_ref: float  # mid al inicio de la ventana (la referencia del salto)
    mid_now: float


class SpilloverTracker:
    """Historia de mids por ticker en ventana rodante; dispara al detectar un salto.

    Puro: el reloj (`now`) lo inyecta el caller (monotonic en producción, determinístico
    en tests). Cooldown POR TICKER: un mismo salto no re-dispara mientras dura su episodio
    (el cooldown por EVENTO — que el ajuste del hermano no dispare un trigger espejo — es
    responsabilidad del shadow, que es quien conoce el parentesco)."""

    def __init__(
        self, *, trigger_move_cents: float, window_sec: float, cooldown_sec: float
    ) -> None:
        self._trigger = trigger_move_cents
        self._window = window_sec
        self._cooldown = cooldown_sec
        self._history: dict[str, deque[tuple[float, float]]] = {}
        self._cooldown_until: dict[str, float] = {}

    def observe(self, ticker: str, mid: float, now: float) -> SpilloverTrigger | None:
        """Registra el mid actual y evalúa el salto contra el inicio de la ventana."""
        hist = self._history.get(ticker)
        if hist is None:
            hist = deque(maxlen=_MAX_POINTS_PER_TICKER)
            self._history[ticker] = hist
        # Poda temporal: la referencia del salto es el punto MÁS VIEJO dentro de la ventana.
        while hist and now - hist[0][0] > self._window:
            hist.popleft()
        hist.append((now, mid))
        if len(hist) < 2:
            return None  # primer punto: no hay contra qué medir
        if now < self._cooldown_until.get(ticker, 0.0):
            return None
        mid_ref = hist[0][1]
        move = mid - mid_ref
        if abs(move) < self._trigger:
            return None
        self._cooldown_until[ticker] = now + self._cooldown
        return SpilloverTrigger(ticker=ticker, move_cents=move, mid_ref=mid_ref, mid_now=mid)
