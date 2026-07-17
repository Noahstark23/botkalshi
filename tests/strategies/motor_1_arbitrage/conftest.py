"""
Fixtures del paquete motor_1_arbitrage.

Motor1Engine._record_edge_window persiste EdgeWindow rows → los tests del engine/detect
necesitan una DB. Esta fixture monta un SQLite temporal como engine global de models,
aislado por test. Los archivos con fixture propia (test_executor) la pisan re-asignando
models._engine y limpiándolo al salir, así que no hay conflicto.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.storage.models as models


@pytest.fixture
def tmp_db_engine(tmp_path):
    """SQLite temporal como engine global de models (opt-in por test)."""
    db = tmp_path / "motor1_tmp.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None
