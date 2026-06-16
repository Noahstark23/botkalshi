"""
CLV Detector (Motor 3, FASE 2) — lógica de time-series PURA para disparar la salida.

Una posición entra en la ventana de salida CLV cuando faltan entre 28 y 30 minutos para
el cierre del mercado. El piso de 28 min (en vez de "<= 30") evita spamear: con el poller
a 60s, un simple "<= 30" dispararía ~30 veces seguidas; la ventana [28, 30] da ~2-3
intentos (reintento si la orden falla) y luego se calla.

SHADOW: el detector solo LOGUEA a nivel INFO; NO ejecuta nada. El executor (FASE 3) y el
lock por-ticker viven aparte. Funciones puras (testeable sin red ni DB).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from src.storage.models import PortfolioPosition

# Ventana de salida: dispara cuando close_time − now ∈ [FLOOR, CEIL].
CLV_EXIT_CEIL = timedelta(minutes=30)  # condición primaria: T-30 del cierre
CLV_EXIT_FLOOR = timedelta(minutes=28)  # piso anti-spam: deja ~2-3 intentos, no 30


def clv_exit_due(position: PortfolioPosition, now: datetime) -> bool:
    """
    True si la posición está en la ventana de salida CLV.

    Condición: CLV_EXIT_FLOOR <= (close_time − now) <= CLV_EXIT_CEIL.
    - close_time None (aún no resuelto por el poller) → False (no se puede decidir).
    - remaining > 30 min → aún no (mercado lejos del cierre).
    - remaining < 28 min (incl. ya cerrado/negativo) → fuera de ventana (no re-dispara).

    `now` debe ser NAIVE UTC (igual que close_time) para no lanzar TypeError.
    """
    if position.close_time is None:
        return False
    remaining = position.close_time - now
    return CLV_EXIT_FLOOR <= remaining <= CLV_EXIT_CEIL


def scan_exits(positions: list[PortfolioPosition], now: datetime) -> list[PortfolioPosition]:
    """Subconjunto de posiciones que están en la ventana de salida CLV ahora."""
    return [p for p in positions if clv_exit_due(p, now)]


def detect_and_log(positions: list[PortfolioPosition], now: datetime) -> list[PortfolioPosition]:
    """
    SHADOW: detecta las salidas CLV debidas y las LOGUEA (no ejecuta). Devuelve la lista
    para que el engine/executor (FASE 3) la consuma cuando MOTOR_3_CLV_ENABLED lo permita.
    """
    due = scan_exits(positions, now)
    for p in due:
        logger.info(f"[MOTOR 3 SHADOW] CLV Exit detectado para {p.ticker}, {p.count} contratos.")
    return due
