"""Fixtures Motor 5: SQLite temporal (el engine persiste quotes/fills/snapshots) +
FairValueBook limpio por test (es ClassVar de proceso)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

import src.storage.models as models
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.utils.config import Settings


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


@pytest.fixture(autouse=True)
def _offline_quote_risk(monkeypatch):
    """Ejercita el gate real con cash ficticio explícito, sin cargar secretos/env.

    Los tests que prueban fallos de capital sobrescriben este estado. No se
    sustituye RiskManager por un permiso universal ni se habilita trading real.
    """
    settings = Settings.model_construct(
        KALSHI_API_KEY_ID="offline-test-key",
        TRADING_ENABLED=False,
        ACTIVE_CAPITAL_USD=1000,
        CAPITAL_CAP_USD=1000,
        CAPITAL_FLOOR_USD=100,
        CAPITAL_SAFETY_FACTOR_PCT=90,
    )
    monkeypatch.setattr("src.risk.manager.get_settings", lambda: settings)
    monkeypatch.setattr(RiskManager, "_check_lock", asyncio.Lock())
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", 1000.0)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 1000.0)
    monkeypatch.setattr(RiskManager, "_last_balance_at", datetime.now(UTC).replace(tzinfo=None))
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    monkeypatch.setattr(Motor5Executor, "_locks", {})
