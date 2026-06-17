"""
Fixture autouse del paquete motor_3_clv: SQLite temporal como engine global de models,
aislado por test. Necesaria porque el executor/poller PERSISTEN Trade/PortfolioPosition.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.storage.models as models


@pytest.fixture(autouse=True)
def _tmp_db_engine(tmp_path):
    db = tmp_path / "motor3_tmp.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None
