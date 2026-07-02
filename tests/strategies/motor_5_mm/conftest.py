"""Fixtures Motor 5: SQLite temporal (el engine persiste quotes/fills/snapshots) +
FairValueBook limpio por test (es ClassVar de proceso)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.storage.models as models
from src.strategies.fair_value_book import FairValueBook


@pytest.fixture(autouse=True)
def _tmp_db_engine(tmp_path):
    db = tmp_path / "motor5_tmp.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


@pytest.fixture(autouse=True)
def _clean_fair_book():
    FairValueBook.clear()
    yield
    FairValueBook.clear()
