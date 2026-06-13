"""Tests de configuración."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.utils.config import Settings, reset_settings_for_testing


@pytest.fixture(autouse=True)
def reset_settings():
    """Reset singleton entre tests."""
    reset_settings_for_testing()
    yield
    reset_settings_for_testing()


@pytest.fixture
def fake_key(tmp_path: Path) -> Path:
    """Crea un archivo dummy para satisfacer key_must_exist."""
    p = tmp_path / "key.pem"
    p.write_text("dummy")
    return p


def test_settings_loads_minimum_required(fake_key: Path, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))

    s = Settings()
    assert s.KALSHI_ENV == "demo"
    assert s.KALSHI_API_KEY_ID == "test-id-12345"
    assert not s.is_production


def test_settings_rejects_short_api_key(fake_key: Path, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "short")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))

    with pytest.raises(ValidationError):
        Settings()


def test_settings_rejects_missing_key_file(monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/nonexistent/path.pem")

    with pytest.raises(ValidationError, match="no encontrada"):
        Settings()


def test_production_safety_caps_capital(fake_key: Path, monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("ACTIVE_CAPITAL_USD", "10000")  # > $5k cap

    with pytest.raises(ValidationError, match="excede límite"):
        Settings()


def test_production_requires_motor_enabled_if_trading(fake_key: Path, monkeypatch):
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    # Ningún MOTOR_X_*_ENABLED activo

    with pytest.raises(ValidationError, match="ningún motor"):
        Settings()


def test_url_property_switches_with_env(fake_key: Path, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))

    monkeypatch.setenv("KALSHI_ENV", "demo")
    s_demo = Settings()
    assert "demo" in s_demo.rest_url
    assert "demo" in s_demo.ws_url

    reset_settings_for_testing()

    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("ACTIVE_CAPITAL_USD", "300")
    s_prod = Settings()
    assert "demo" not in s_prod.rest_url
    assert s_prod.is_production


def test_telegram_configured_helper(fake_key: Path, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))

    s_no_tg = Settings()
    assert not s_no_tg.telegram_configured

    reset_settings_for_testing()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:xyz")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    s_with_tg = Settings()
    assert s_with_tg.telegram_configured
