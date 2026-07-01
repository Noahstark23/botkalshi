"""
Fixtures del paquete motor_2_consensus.

El poller shadow persiste EdgeWindow(kind="consensus") cuando la fuente de odds es real
(is_live). Esta fixture autouse monta un SQLite temporal como engine global de models,
aislado por test, para que el path de persistencia no falle ni toque la DB real.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.storage.models as models


@pytest.fixture(autouse=True)
def _tmp_db_engine(tmp_path):
    db = tmp_path / "motor2_tmp.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


@pytest.fixture(autouse=True)
def _clear_shared_exit_locks():
    """Los locks por-ticker de Motor3ExitExecutor son ClassVar (compartidos a nivel
    proceso): un test que dejara un lock tomado envenenaría con 'busy' a todo test
    posterior del mismo ticker. Se limpian entre tests."""
    from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

    Motor3ExitExecutor._locks.clear()
    yield
    Motor3ExitExecutor._locks.clear()
