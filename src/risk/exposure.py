"""Una sola matemática de exposición, sin inferir cierres ni inventar fills."""

from __future__ import annotations

from fractions import Fraction
from math import ceil

from src.storage.models import Trade
from src.strategies.motor_5_mm.ledger import confirmed_fill
from src.strategies.motor_5_mm.order_state import COUNT_QUARANTINE_NOTE

MM_STRATEGY = "motor_5_mm"


class UnknownExposureError(ValueError):
    """Falta evidencia para autorizar riesgo adicional."""


def _integer(value: object, *, minimum: int, field: str) -> int:
    if type(value) is not int or value < minimum:
        raise UnknownExposureError(f"{field} inválido")
    return value


def contract_risk_cents(*, action: str, side: str, price_cents: int) -> int:
    """BUY arriesga P; SELL abre el lado opuesto por 100-P sin atribución de cierre.

    El precio siempre corresponde a ``side``. Vender YES a 5¢ y comprar NO a 95¢
    tienen la misma pérdida contractual máxima. No se netean inventarios ajenos.
    """
    if action not in ("buy", "sell") or side not in ("yes", "no"):
        raise UnknownExposureError("action/side desconocido")
    price = _integer(price_cents, minimum=1, field="price_cents")
    if price > 99:
        raise UnknownExposureError("price_cents fuera de [1,99]")
    return price if action == "buy" else 100 - price


def exposure_count(trade: Trade) -> int | Fraction:
    """Pending reserva íntegro; filled M5 exige cantidad confirmada persistida.

    Los FOK/IOC de otros motores conservan la semántica legacy documentada del
    modelo: sin filled_count, su estado filled representa count completo.
    """
    if trade.strategy == MM_STRATEGY and COUNT_QUARANTINE_NOTE in (trade.notes or ""):
        # La evidencia puede exceder count; tampoco otros motores pueden asumir
        # que la reserva original alcanza mientras el ledger está en cuarentena.
        raise UnknownExposureError("M5 con cantidad en cuarentena")
    count = _integer(trade.count, minimum=1, field="count")
    try:
        filled = confirmed_fill(trade) if trade.strategy == MM_STRATEGY else trade.filled_count
    except ValueError as exc:
        raise UnknownExposureError("cantidad M5 persistida inválida") from exc
    if filled is not None:
        if trade.strategy != MM_STRATEGY:
            filled = _integer(filled, minimum=0, field="filled_count")
        if filled > count:
            raise UnknownExposureError("filled_count excede count")
    if trade.status == "pending":
        return count
    if trade.status != "filled":
        raise UnknownExposureError(f"estado abierto desconocido: {trade.status}")
    if filled is None:
        if trade.strategy == MM_STRATEGY:
            raise UnknownExposureError("M5 filled sin cantidad confirmada")
        return count
    return filled


def open_risk_cents(trade: Trade) -> int:
    # El headroom legacy está en centavos enteros: reservar hacia arriba, jamás
    # perder la exposición de una fracción. La cantidad original sigue exacta.
    return ceil(
        contract_risk_cents(action=trade.action, side=trade.side, price_cents=trade.price_cents)
        * exposure_count(trade)
    )


def committed_risk_cents(trade: Trade) -> int:
    """Presupuesto diario: reservar una orden consume su cantidad solicitada.

    No se repone al cancelar/cerrar; conserva esa política del experimento y
    corrige el precio complementario de las ventas de M5.
    """
    return contract_risk_cents(
        action=trade.action, side=trade.side, price_cents=trade.price_cents
    ) * _integer(trade.count, minimum=1, field="count")
