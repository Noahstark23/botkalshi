"""Bloqueo durable de M5 y dueño de sesión; dos filas, nunca una lista sin tope.

Un barrido vacío no explica una orden perdida. El incidente solo se reconoce
manualmente, con el servicio detenido y evidencia externa revisada por el operador.
La sesión activa es un marcador previo a cualquier orden: un kill -9/OOM deja
evidencia para el próximo arranque aun si no alcanzó a registrar el incidente.
"""

from datetime import UTC, datetime
from uuid import uuid4

from loguru import logger
from sqlmodel import or_, select, update

from src.storage.models import OperationalState, Trade, get_session
from src.strategies.motor_5_mm.order_state import COUNT_QUARANTINE_NOTE

INCIDENT_KEY = "mm_execution_incident"
SESSION_KEY = "mm_execution_session"
ACKNOWLEDGEMENT = "ACK M5 RECONCILED"
# Reconoce exclusivamente al dueño sano de ESTE proceso. No es una lease
# distribuida: al reiniciar, active en DB con owner desconocido bloquea todos los gates.
_current_execution_owner: str | None = None


def _latch(session, reason: str) -> None:
    row = session.get(OperationalState, INCIDENT_KEY)
    if row is None:
        row = OperationalState(key=INCIDENT_KEY, value="blocked", reason=reason[:500])
    elif row.value == "clear":
        row.value = "blocked"
        row.reason = reason[:500]
        row.updated_at = datetime.now(UTC)
    session.add(row)


def latch_incident(reason: str) -> None:
    """Conserva el primer motivo; un fallo de DB se propaga para frenar el caller."""
    global _current_execution_owner
    # Ni recuperar la DB tras un write fallido vuelve sana una sesión que falló.
    _current_execution_owner = None
    with get_session() as session:
        _latch(session, reason)
        session.commit()
    logger.error(f"motor5.incident_latched requiere revisión manual: {reason[:500]}")


def incident_status() -> tuple[bool, str | None]:
    """Incidente, sesión abandonada o DB ilegible bloquean aun si M5 está apagado."""
    try:
        with get_session() as session:
            row = session.get(OperationalState, INCIDENT_KEY)
            if row is not None and row.value != "clear":
                return True, row.reason or "incidente M5 sin motivo; requiere revisión manual"
            active = session.get(OperationalState, SESSION_KEY)
            if active is None or active.value == "clean":
                return False, None
            if (
                active.value == "active"
                and _current_execution_owner is not None
                and active.reason == _current_execution_owner
            ):
                return False, None
            return True, "sesión M5 sin dueño sano en este proceso; requiere revisión manual"
    except Exception:
        logger.exception("motor5.incident_read_failed → nuevas órdenes bloqueadas")
        return True, "estado durable M5 inaccesible; requiere revisión manual"


def begin_execution_session() -> str:
    """El marcador se confirma en DB ANTES de construir el executor."""
    global _current_execution_owner
    owner = str(uuid4())
    with get_session() as session:
        row = session.get(OperationalState, SESSION_KEY)
        if row is not None and row.value != "clean":
            _latch(session, "sesión M5 anterior sin cierre confirmado; requiere revisión manual")
        if row is None:
            row = OperationalState(key=SESSION_KEY, value="active", reason=owner)
        else:
            row.value = "active"
            row.reason = owner
            row.updated_at = datetime.now(UTC)
        session.add(row)
        session.commit()
    _current_execution_owner = owner
    return owner


def finish_execution_session(owner: str) -> None:
    """Solo el dueño actual cierra su sesión tras cancelar y reconciliar íntegramente.

    El caller debe comprobar snapshot completo y ningún coid runtime restante. La DB
    es una segunda barrera independiente; los fills terminales siguen en el ledger.
    """
    global _current_execution_owner
    with get_session() as session:
        row = session.get(OperationalState, SESSION_KEY)
        if row is None or row.value != "active" or row.reason != owner:
            raise RuntimeError("M5 no puede cerrar una sesión que no posee")
        pending = session.exec(
            select(Trade).where(Trade.strategy == "motor_5_mm", Trade.status == "pending")
        ).first()
        if pending is not None:
            raise RuntimeError("M5 conserva intenciones pending; sesión no cerrada")
        changed = session.exec(
            update(OperationalState)
            .where(
                OperationalState.key == SESSION_KEY,
                OperationalState.value == "active",
                OperationalState.reason == owner,
            )
            .values(value="clean", updated_at=datetime.now(UTC))
        )
        if changed.rowcount != 1:
            raise RuntimeError("el dueño de sesión M5 cambió durante el cierre")
        session.commit()
    if _current_execution_owner == owner:
        _current_execution_owner = None


def acknowledge_incident(*, review_reference: str, confirmation: str) -> None:
    """Solo operador OFFLINE tras revisar órdenes/posiciones/fills externos completos.

    No verifica que el servicio esté detenido: esa precondición debe garantizarla el
    operador. No permite reconocer una sesión active ni borra historial/flags/órdenes.
    Una DB vacía NO constituye evidencia externa de ausencia de exposición.
    """
    if confirmation != ACKNOWLEDGEMENT or not review_reference.strip():
        raise ValueError("se exige referencia de revisión y ACK M5 RECONCILED literal")
    with get_session() as session:
        active = session.get(OperationalState, SESSION_KEY)
        if active is not None and active.value != "clean":
            raise RuntimeError("sesión M5 no cerrada; no se puede reconocer el incidente")
        unresolved = session.exec(
            select(Trade).where(
                Trade.strategy == "motor_5_mm",
                or_(
                    Trade.status.in_(["pending", "filled"]),
                    Trade.notes.contains(COUNT_QUARANTINE_NOTE),
                ),
            )
        ).first()
        if unresolved is not None:
            raise RuntimeError("M5 conserva exposición o intenciones sin resolver")
        row = session.get(OperationalState, INCIDENT_KEY)
        if row is not None:
            row.value = "clear"
            row.reason = f"revisión manual: {review_reference.strip()}"[:500]
            row.updated_at = datetime.now(UTC)
            session.add(row)
            session.commit()
