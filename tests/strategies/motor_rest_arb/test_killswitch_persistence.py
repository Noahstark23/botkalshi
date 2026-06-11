"""
Tests del kill-switch PERSISTENTE (PR-A.3): sobrevive restarts de Coolify.

Cubre la API (engage/kill_switch_engaged/clear, idempotente, round-trip contra SQLite
real temporal) y que AMBOS kill-switches persisten: RiskManager._trigger_kill_switch
(stop-loss) y RestExecutor._fire_kill_switch (rollback no convergió). El executor además
eleva la pausa a BotState (el bonus gap: antes solo seteaba _paused en memoria).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlmodel import Session

import src.storage.models as models
from src.monitoring.health import BotState


@pytest.fixture
def sqlite_engine(tmp_path):
    """Engine SQLite temporal como singleton de models (engage/clear lo usan vía get_session)."""
    db = tmp_path / "ks.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


@pytest.fixture(autouse=True)
def _reset_botstate():
    """Aísla BotState entre tests (es estado de clase global)."""
    prev = (BotState.is_paused, BotState.pause_reason)
    BotState.is_paused, BotState.pause_reason = False, None
    yield
    BotState.is_paused, BotState.pause_reason = prev


# =====================================================
# API de persistencia
# =====================================================


def test_engage_then_read_roundtrip(sqlite_engine):
    assert models.kill_switch_engaged() == (False, None)  # vacío → no engaged
    models.engage_kill_switch("rollback no convergió")
    assert models.kill_switch_engaged() == (True, "rollback no convergió")


def test_engage_is_idempotent_single_row(sqlite_engine):
    models.engage_kill_switch("razón 1")
    models.engage_kill_switch("razón 2")  # upsert, no segunda fila
    with Session(sqlite_engine) as s:
        count = s.exec(text("SELECT count(*) FROM operational_state")).scalar()
    assert count == 1
    assert models.kill_switch_engaged() == (True, "razón 2")  # quedó la última razón


def test_clear_disengages(sqlite_engine):
    models.engage_kill_switch("x")
    models.clear_kill_switch()
    assert models.kill_switch_engaged() == (False, None)


# =====================================================
# Ambos kill-switches persisten
# =====================================================


@pytest.mark.asyncio
async def test_risk_manager_kill_switch_persists(sqlite_engine):
    """RiskManager._trigger_kill_switch → engage en DB + BotState (sin cambiar queries)."""
    from src.risk.manager import RiskManager

    with patch("src.risk.manager.get_settings", return_value=MagicMock()), patch(
        "src.risk.manager.alert_risk_event", new=AsyncMock()
    ):
        rm = RiskManager()
        await rm._trigger_kill_switch("Stop-Loss Diario superado")

    assert BotState.is_paused is True
    assert models.kill_switch_engaged() == (True, "Stop-Loss Diario superado")


@pytest.mark.asyncio
async def test_executor_kill_switch_persists_and_sets_botstate(sqlite_engine):
    """RestExecutor._fire_kill_switch → engage en DB + eleva la pausa a BotState (bonus gap)."""
    from src.strategies.motor_rest_arb.executor import RestExecutor

    ex = RestExecutor(MagicMock())
    leg_result = MagicMock()
    leg_result.leg.market_ticker = "KXTEST"
    with patch("src.monitoring.telegram_alerts.alert_error", new=AsyncMock()):
        await ex._fire_kill_switch([leg_result])

    assert BotState.is_paused is True
    engaged, reason = models.kill_switch_engaged()
    assert engaged is True
    assert "KXTEST" in reason
