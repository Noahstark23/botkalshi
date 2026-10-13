"""Marca durable pre-DELETE; no confirma cancelación ni libera reservas."""

import time

from sqlalchemy import text
from sqlmodel import select

from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.incidents import latch_incident


class CancelIntentMismatchError(ValueError):
    """El ledger ya no permite atribuir el DELETE; no es confirmación terminal."""


def record_cancel_requests(targets: list[tuple[str, str]], *, now_ms: int | None = None) -> None:
    """Todos los coid/order_id se verifican y marcan en un único commit.

    El reemplazo normal no envía DELETE si falla. El retiro protector usa
    prepare_protective_cancel. No se mantiene un lock durante red.
    Repetir una solicitud conserva la primera fecha; una lectura resting, edad,
    ausencia o reinicio nunca la elimina. Sólo evidencia terminal puede cambiar
    el status de la orden mediante los escritores de confirmación existentes.
    NULL legacy no acredita ausencia de cancelaciones históricas.
    """
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    if type(now_ms) is not int or now_ms <= 0 or not 1 <= len(targets) <= 1000:
        raise ValueError("INVALID_CANCEL_INTENT")
    if (
        any(
            not isinstance(coid, str) or not coid or not isinstance(oid, str) or not oid
            for coid, oid in targets
        )
        or len({coid for coid, _ in targets}) != len(targets)
        or len({oid for _, oid in targets}) != len(targets)
    ):
        raise ValueError("INVALID_CANCEL_IDENTITY")
    with get_session() as session:
        session.exec(text("BEGIN IMMEDIATE"))
        if not session.connection().connection.driver_connection.in_transaction:
            raise ValueError("CANCEL_INTENT_WITHOUT_TRANSACTION")
        for coid, oid in targets:
            row = session.exec(select(Trade).where(Trade.client_order_id == coid)).first()
            if (
                row is None
                or row.strategy != "motor_5_mm"
                or row.status != "pending"
                or row.kalshi_order_id != oid
            ):
                raise CancelIntentMismatchError("CANCEL_INTENT_IDENTITY_MISMATCH")
            previous = row.cancel_requested_at_ms
            if previous is not None and (type(previous) is not int or not 0 < previous <= now_ms):
                raise ValueError("INVALID_CANCEL_INTENT_CLOCK")
            if previous is None:
                row.cancel_requested_at_ms = now_ms
                session.add(row)
        session.commit()


def prepare_protective_cancel(coid: str, oid: str) -> tuple[bool, Exception | None]:
    """Intent por objetivo: un fallo no deja sin retirar otras quotes conocidas.

    Un mismatch excluye ese objetivo; un fallo de escritura/reloj permite intentar
    su DELETE, pero devuelve el error para que el supervisor no cierre sano. Antes
    revoca el dueño en memoria e intenta enclavar incidente durable. Si la DB está
    caída, la sesión activa previa sigue bloqueando al reiniciar. Ningún resultado
    de esta función confirma un terminal ni libera reservas.
    """
    try:
        record_cancel_requests([(coid, oid)])
    except Exception as exc:
        can_send = (
            not isinstance(exc, CancelIntentMismatchError)
            and isinstance(coid, str)
            and bool(coid.strip())
            and isinstance(oid, str)
            and bool(oid.strip())
        )
        try:
            latch_incident("intent protector M5 no confirmado; requiere revisión")
        except Exception as latch_error:
            return can_send, latch_error
        return can_send, exc if can_send else None
    return True, None
