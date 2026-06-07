"""
Tests del fix de concurrencia SQLite (WAL + busy_timeout) en get_engine.

Verifica que:
  - la DB queda en journal_mode=WAL y busy_timeout=5000 tras conectar;
  - dos escritores concurrentes (V1 + shadow) NO disparan 'database is locked'.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlmodel import Session

import src.storage.models as models


@pytest.fixture
def sqlite_engine(tmp_path):
    """Engine fresco sobre una DB SQLite temporal, con los PRAGMAs del fix aplicados."""
    db = tmp_path / "test_wal.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    # Reset del singleton para que get_engine construya uno nuevo con esta URL.
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    yield engine
    models._engine = None


def test_pragmas_applied_wal_and_busy_timeout(sqlite_engine):
    """Tras conectar, la DB está en WAL con busy_timeout=5000."""
    with sqlite_engine.connect() as conn:
        journal = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
    assert str(journal).lower() == "wal"
    assert int(busy) == 5000


def test_two_concurrent_writers_no_database_locked(sqlite_engine):
    """Dos sesiones escribiendo (simula V1 + shadow) no lanzan 'database is locked'."""
    models.SQLModel.metadata.create_all(sqlite_engine)

    # Sesión A abre transacción y escribe; sesión B escribe en paralelo.
    # Con journal_mode=delete + busy_timeout=0 esto tiende a 'database is locked';
    # con WAL + busy_timeout=5000 ambas completan.
    sess_a = Session(sqlite_engine)
    sess_b = Session(sqlite_engine)
    try:
        sess_a.add(models.EdgeWindow(market_ticker="A", magnitude_cents=1))
        sess_a.commit()
        sess_b.add(models.OrderbookEvent(ticker="B", side="yes", price_cents=40, delta=10))
        sess_b.commit()  # no debe lanzar OperationalError: database is locked
    finally:
        sess_a.close()
        sess_b.close()

    # Ambos registros persistieron.
    with Session(sqlite_engine) as s:
        assert s.exec(text("SELECT count(*) FROM edge_windows")).scalar() == 1
        assert s.exec(text("SELECT count(*) FROM orderbook_events")).scalar() == 1
