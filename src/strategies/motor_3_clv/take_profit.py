"""
Take-Profit detector (Motor 3, FASE 1) — salida por PRECIO, no por tiempo.

Complementa `clv_exit_due` (ventana T-30min en detector.py) con una segunda condición: si el
lado que tenemos abierto cotiza a un bid >= umbral, la posición va ganando "demasiado" y el
riesgo de remontada supera el upside restante → cerrar y asegurar. El análisis histórico
(1.620 trades, closed_by_clv=0) mostró que el 100% iba a settlement; un ticket al ~90% se
remontó hasta pérdida total. El take-profit corta ese caso.

Función pura (espejo de `clv_exit_due`): recibe el bid YA resuelto (el engine lo trae del
orderbook), no toca red ni DB → testeable sin mocks.
"""

from __future__ import annotations

from src.storage.models import PortfolioPosition

# Umbral de take-profit en cents. Calibrable desde Coolify (MOTOR_3_TAKE_PROFIT_CENTS).
# 90c = asegurar cuando la posición ya "casi ganó". Trade-off: deja ~10c de upside en las que
# sí ganaban, a cambio de eliminar el riesgo de remontada (ver análisis histórico).
DEFAULT_TAKE_PROFIT_CENTS = 90


def take_profit_due(
    position: PortfolioPosition,
    current_bid_cents: int | None,
    threshold_cents: int = DEFAULT_TAKE_PROFIT_CENTS,
) -> bool:
    """True si el bid del lado abierto alcanzó el umbral de take-profit.

    - bid None (sin liquidez / orderbook no resuelto) → False (no se puede decidir).
    - count <= 0 (posición ya cerrada o inconsistente) → False.
    - bid >= umbral → True (asegurar).
    """
    if current_bid_cents is None:
        return False
    if position.count <= 0:
        return False
    return current_bid_cents >= threshold_cents
