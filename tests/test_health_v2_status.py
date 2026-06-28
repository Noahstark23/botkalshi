"""
Tests for /status endpoint — orderbook_manager_v2 section.

Covers: flag=False → {"enabled": false}, flag=True + instance present → full metrics,
flag=True + instance missing → {"enabled": true, "instance": "missing"} + record_error.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.monitoring.health import BotState, app
from src.risk.manager import RiskManager


@pytest.fixture(autouse=True)
def reset_botstate():
    BotState.v2_manager = None
    BotState.last_error = None
    BotState.last_error_at = None
    yield
    BotState.v2_manager = None
    BotState.last_error = None


def mock_settings(*, v2_enabled: bool):
    s = MagicMock()
    s.USE_ORDERBOOK_MANAGER_V2 = v2_enabled
    s.KALSHI_ENV = "production"
    s.TRADING_ENABLED = False
    s.ACTIVE_CAPITAL_USD = 300.0
    s.MOTOR_1_ARBITRAGE_ENABLED = False
    s.MOTOR_2_SPORTSBOOK_ENABLED = False
    s.MOTOR_3_CLV_ENABLED = False
    return s


def mock_db_session():
    """Patch get_session to return a context manager with empty query results."""
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.exec.return_value.all.return_value = []
    session.exec.return_value.first.return_value = None
    return session


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=True)


def test_status_shows_v2_disabled_when_flag_false(client):
    """USE_ORDERBOOK_MANAGER_V2=False → orderbook_manager_v2: {enabled: false}."""
    session = mock_db_session()
    with (
        patch("src.monitoring.health.get_settings", return_value=mock_settings(v2_enabled=False)),
        patch("src.monitoring.health.get_session", return_value=session),
    ):
        resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    v2 = data["orderbook_manager_v2"]
    assert v2 == {"enabled": False}


def test_status_shows_v2_metrics_when_enabled(client):
    """USE_ORDERBOOK_MANAGER_V2=True + instance present → full metrics object."""
    mock_mgr = MagicMock()
    mock_mgr.stats.return_value = {
        "initialized_tickers": 38,
        "gaps_last_60s": 2,
        "last_gap_at": "2026-05-22T10:00:00+00:00",
    }
    mock_mgr._tickers_by_sid = {1: {"A", "B", "C"}}
    mock_mgr._recovering = set()

    BotState.v2_manager = mock_mgr

    session = mock_db_session()
    with (
        patch("src.monitoring.health.get_settings", return_value=mock_settings(v2_enabled=True)),
        patch("src.monitoring.health.get_session", return_value=session),
    ):
        resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    v2 = data["orderbook_manager_v2"]

    assert v2["enabled"] is True
    assert v2["books_initialized"] == 38
    assert v2["sids_tracked"] == 1
    assert v2["sids_recovering"] == 0
    assert v2["gaps_last_60s"] == 2
    assert v2["last_gap_at"] == "2026-05-22T10:00:00+00:00"


def test_status_handles_missing_manager_gracefully(client):
    """USE_ORDERBOOK_MANAGER_V2=True but BotState.v2_manager=None → instance: missing + record_error."""
    BotState.v2_manager = None  # flag says enabled but instance missing

    session = mock_db_session()
    with (
        patch("src.monitoring.health.get_settings", return_value=mock_settings(v2_enabled=True)),
        patch("src.monitoring.health.get_session", return_value=session),
    ):
        resp = client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    v2 = data["orderbook_manager_v2"]

    assert v2["enabled"] is True
    assert v2["instance"] == "missing"
    # BotState.record_error must have been called
    assert BotState.last_error is not None
    assert "missing" in BotState.last_error


def test_status_includes_capital_block(client):
    """El /status incluye el bloque capital (mode/raw/effective/is_paused)."""
    cap_settings = mock_settings(v2_enabled=False)
    cap_settings.DYNAMIC_CAPITAL_ENABLED = True
    cap_settings.ACTIVE_CAPITAL_USD = 300.0
    cap_settings.CAPITAL_SAFETY_FACTOR_PCT = 90.0
    cap_settings.CAPITAL_FLOOR_USD = 100.0
    cap_settings.CAPITAL_CAP_USD = 2000.0

    session = mock_db_session()
    RiskManager._cached_capital_usd = None  # sin balance real → effective = ACTIVE_CAPITAL_USD
    RiskManager._last_raw_balance_usd = None
    try:
        with (
            patch(
                "src.monitoring.health.get_settings",
                return_value=mock_settings(v2_enabled=False),
            ),
            patch("src.monitoring.health.get_session", return_value=session),
            patch("src.risk.manager.get_settings", return_value=cap_settings),
        ):
            resp = client.get("/status")
    finally:
        RiskManager._cached_capital_usd = None
        RiskManager._last_raw_balance_usd = None

    assert resp.status_code == 200
    cap = resp.json()["capital"]
    assert cap["mode"] == "dynamic"
    assert cap["effective_usd"] == 300.0  # fallback a ACTIVE_CAPITAL_USD (sin cash real aún)
    assert cap["is_paused"] is False
