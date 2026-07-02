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


def test_execution_flags_do_not_count_as_enabled_motor(fake_key: Path, monkeypatch):
    """Deuda auditoría 2026-07-01: MOTOR_3_EXECUTION_ENABLED solo no arranca ningún motor
    — el guard 'ningún motor habilitado' debe rechazarlo en producción (antes pasaba y
    el bot booteaba 'válido' sin ningún motor corriendo)."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_3_EXECUTION_ENABLED", "true")

    with pytest.raises(ValidationError, match="ningún motor"):
        Settings()


def test_execution_without_engine_flag_rejected(fake_key: Path, monkeypatch):
    """EXECUTION=true sin el motor corriendo es un no-op engañoso → fail-fast al boot."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_2_SPORTSBOOK_ENABLED", "true")
    monkeypatch.setenv("MOTOR_3_EXECUTION_ENABLED", "true")

    with pytest.raises(ValidationError, match="MOTOR_3_CLV_ENABLED"):
        Settings()


def test_motor_mm_execution_flag_rejected_in_production_f1(fake_key: Path, monkeypatch):
    """Motor 5 está en F1 (shadow, sin executor): EXECUTION=true sería un no-op engañoso
    que PARECE armado → fail-loud al boot hasta F2 (plan motor_5 §4)."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("MOTOR_MM_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_EXECUTION_ENABLED", "true")

    with pytest.raises(ValidationError, match="F1"):
        Settings()


def test_motor_mm_alone_does_not_count_as_enabled_motor(fake_key: Path, monkeypatch):
    """MOTOR_MM_ENABLED en F1 no puede operar capital → NO satisface el guard 'ningún
    motor habilitado' con TRADING_ENABLED=true (misma regla que los *_EXECUTION)."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_ENABLED", "true")

    with pytest.raises(ValidationError, match="ningún motor"):
        Settings()


def test_motor_mm_shadow_config_valid_in_production(fake_key: Path, monkeypatch):
    """El modo F1 legítimo: ENABLED=true + EXECUTION=false bootea (shadow junto a un
    motor real cualquiera)."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_2_SPORTSBOOK_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_ENABLED", "true")

    s = Settings()
    assert s.MOTOR_MM_ENABLED and not s.MOTOR_MM_EXECUTION_ENABLED
