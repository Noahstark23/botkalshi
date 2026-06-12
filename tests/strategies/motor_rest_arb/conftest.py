"""
Fixtures compartidas del paquete motor_rest_arb.

Desde A.1, RestExecutor.execute() PERSISTE Trade rows (intents pre-red + updates por
estado) → cualquier test que ejecute execute() necesita una DB. Esta fixture autouse
monta un SQLite temporal como singleton de models para TODO el paquete, así los tests
de rutas no fallan por falta de engine (y ninguno toca la DB real).

Los archivos con fixture propia (test_capa3_outcome, test_killswitch_persistence) la
pisan tranquilamente: re-asignan models._engine y lo limpian al salir.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.storage.models as models


@pytest.fixture(autouse=True)
def _tmp_db_engine(tmp_path):
    """SQLite temporal como engine global de models (aislado por test)."""
    db = tmp_path / "pkg_tmp.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None
