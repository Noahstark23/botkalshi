"""Lecturas de gates: sin autoflush ni confirmación de cambios del caller."""

from contextlib import contextmanager

from sqlmodel import Session


@contextmanager
def financial_read_scope(session: Session):
    """El caller conserva su transacción, lock, commit y rollback.

    Una fila aún no enviada a SQLite podría quedar fuera de un SELECT o de sus
    filtros. No la ignoramos ni hacemos flush desde una lectura: es desconocido.
    Los metadatos del adaptador de permisos no son filas financieras y pueden
    estar pendientes. El caller debe leer después de adquirir su lock y antes de
    añadir la nueva reserva. Esto no acredita conciliación externa ni cuenta vacía.
    """
    from src.storage.models import OperationalState, PortfolioPosition, RiskCommitment, Trade

    models = (OperationalState, PortfolioPosition, RiskCommitment, Trade)
    pending = list(session.new) + list(session.deleted)
    pending.extend(row for row in session.dirty if session.is_modified(row))
    if any(isinstance(row, models) for row in pending):
        raise ValueError("UNFLUSHED_FINANCIAL_STATE")
    with session.no_autoflush:
        yield session
