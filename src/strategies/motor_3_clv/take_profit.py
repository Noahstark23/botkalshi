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

from src.math.fees import kalshi_fee_cents
from src.storage.models import PortfolioPosition

# Umbral de take-profit en cents. Calibrable desde Coolify (MOTOR_3_TAKE_PROFIT_CENTS).
# 90c = asegurar cuando la posición ya "casi ganó". Trade-off: deja ~10c de upside en las que
# sí ganaban, a cambio de eliminar el riesgo de remontada (ver análisis histórico).
DEFAULT_TAKE_PROFIT_CENTS = 90


def take_profit_due(
    position: PortfolioPosition,
    current_bid_cents: int | None,
    threshold_cents: int = DEFAULT_TAKE_PROFIT_CENTS,
    *,
    entry_cents: int | None,
) -> bool:
    """True si el bid alcanzó el umbral Y vender deja ganancia NETA sobre el entry.

    El TP existe para ASEGURAR una posición casi ganada — nunca para vender por precio
    absoluto (fix auditoría 2026-07-01: sin comparar contra el entry, toda entrada comprada
    >= umbral — favoritos de consenso a 90c+ — se auto-liquidaba en pérdida al tick
    siguiente de abrirse: "take-profit" convertido en stop-loss instantáneo sistemático).

    - bid None (sin liquidez / orderbook no resuelto) → False (no se puede decidir).
    - count <= 0 (posición ya cerrada o inconsistente) → False.
    - entry_cents None (no derivable de las patas BUY) → False (no vender a ciegas).
    - bid >= umbral Y (bid − entry) − fees ida y vuelta (1 contrato) > 0 → True.
    """
    if current_bid_cents is None:
        return False
    if position.count <= 0:
        return False
    if entry_cents is None:
        return False
    if current_bid_cents < threshold_cents:
        return False
    net_per_contract = (
        current_bid_cents
        - entry_cents
        - kalshi_fee_cents(1, current_bid_cents)
        - kalshi_fee_cents(1, entry_cents)
    )
    return net_per_contract > 0
