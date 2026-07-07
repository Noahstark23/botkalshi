"""
Auditoría 2026-07-07 (P1): POST /admin/resume NO puede saltarse el kill-switch persistente.

Un curl al endpoint levantaba CUALQUIER pausa — incluida la de un stop-loss o un rollback
abortado — sin la verificación de posiciones=0 de scripts/clear_kill_switch.py. Ahora:
kill-switch engaged → 409 (y el switch queda intacto); DB ilegible → 503 (fail-closed);
pausa runtime simple → resume normal (sin cambios de comportamiento).
La fixture autouse del conftest monta un SQLite temporal por test.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.monitoring.health import BotState, app
from src.storage.models import engage_kill_switch, kill_switch_engaged


@pytest.fixture(autouse=True)
def reset_bot_state():
    BotState.is_paused = False
    BotState.pause_reason = None
    yield
    BotState.is_paused = False
    BotState.pause_reason = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_resume_normal_pause_still_works(client):
    """CONTROL: pausa runtime simple (sin kill-switch) → resume como siempre."""
    BotState.is_paused = True
    BotState.pause_reason = "verificando algo"

    resp = client.post("/admin/resume")

    assert resp.status_code == 200
    assert resp.json() == {"status": "running"}
    assert BotState.is_paused is False


def test_resume_not_paused_is_noop(client):
    resp = client.post("/admin/resume")
    assert resp.status_code == 200
    assert resp.json() == {"status": "already_running"}


def test_resume_refused_when_kill_switch_engaged(client):
    """Kill-switch persistente engaged → 409, el bot SIGUE pausado y el switch intacto
    (solo scripts/clear_kill_switch.py lo levanta)."""
    engage_kill_switch("daily_stop_loss: -$45.00")
    BotState.is_paused = True
    BotState.pause_reason = "daily_stop_loss"

    resp = client.post("/admin/resume")

    assert resp.status_code == 409
    assert "clear_kill_switch" in resp.json()["detail"]
    assert BotState.is_paused is True  # nada cambió
    engaged, reason = kill_switch_engaged()
    assert engaged is True and "daily_stop_loss" in (reason or "")


def test_resume_fails_closed_when_db_unreadable(client):
    """FAIL-SAFE: si no se puede verificar el kill-switch, el resume se DENIEGA (503) —
    ante la duda, el bot queda pausado."""
    BotState.is_paused = True
    BotState.pause_reason = "algo"

    with patch("src.storage.models.kill_switch_engaged", side_effect=RuntimeError("db locked")):
        resp = client.post("/admin/resume")

    assert resp.status_code == 503
    assert BotState.is_paused is True
