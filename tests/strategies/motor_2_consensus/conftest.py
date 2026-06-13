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
