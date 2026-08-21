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
    """Sin clave por NINGUNA de las dos vías, el boot rompe.

    ⚠️ CAMBIO SEMÁNTICO DELIBERADO (incidente 2026-08-20): antes bastaba con que
    faltara el ARCHIVO; ahora falta la clave solo si tampoco está KALSHI_PRIVATE_KEY.
    El env vacío se fuerza explícitamente porque el contrato cambió — sin eso, este
    test pasaría o fallaría según lo que hubiera en el entorno del que corre."""
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/nonexistent/path.pem")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY", "")

    with pytest.raises(ValidationError, match="No hay clave privada"):
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


def test_motor1_execution_without_motor_flag_rejected(fake_key: Path, monkeypatch):
    """Capa A por motor (auditoría 2026-07-07): MOTOR_1_EXECUTION_ENABLED sin el motor
    corriendo es un no-op engañoso → fail-fast al boot (mismo patrón que M3/REST)."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_2_SPORTSBOOK_ENABLED", "true")
    monkeypatch.setenv("MOTOR_1_EXECUTION_ENABLED", "true")

    with pytest.raises(ValidationError, match="MOTOR_1_ARBITRAGE_ENABLED"):
        Settings()


def test_motor2_entry_execution_without_motor_flag_rejected(fake_key: Path, monkeypatch):
    """Ídem para la entrada de Motor 2 (distinta de MOTOR_2_EXECUTION_ENABLED, que gatea
    las ventas del brazo de salida)."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_1_ARBITRAGE_ENABLED", "true")
    monkeypatch.setenv("MOTOR_2_ENTRY_EXECUTION_ENABLED", "true")

    with pytest.raises(ValidationError, match="MOTOR_2_SPORTSBOOK_ENABLED"):
        Settings()


def test_motor1_and_motor2_entry_execution_boot_with_motor_flags(fake_key: Path, monkeypatch):
    """CONTROL: con el motor + su flag de entrada, producción bootea (la combinación
    legítima de activación no quedó bloqueada)."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_1_ARBITRAGE_ENABLED", "true")
    monkeypatch.setenv("MOTOR_1_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("MOTOR_2_SPORTSBOOK_ENABLED", "true")
    monkeypatch.setenv("MOTOR_2_ENTRY_EXECUTION_ENABLED", "true")

    s = Settings()
    assert s.MOTOR_1_EXECUTION_ENABLED is True
    assert s.MOTOR_2_ENTRY_EXECUTION_ENABLED is True


def test_motor_mm_execution_in_production_requires_f3_key(fake_key: Path, monkeypatch):
    """PRODUCCIÓN + EXECUTION sin la llave F3 → boot roto (plan §5: el orden importa —
    smoke test primero, luego girar la llave como acto deliberado)."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("MOTOR_MM_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_EXECUTION_ENABLED", "true")

    with pytest.raises(ValidationError, match="llave F3"):
        Settings()


def test_motor_mm_f3_key_wrong_value_still_blocks(fake_key: Path, monkeypatch):
    """La llave exige el valor EXACTO — un typo no activa un market maker."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("MOTOR_MM_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_F3_ACK", "noel-ok-f3")  # case incorrecto

    with pytest.raises(ValidationError, match="llave F3"):
        Settings()


def test_motor_mm_f3_key_unlocks_production(fake_key: Path, monkeypatch):
    """Con la llave exacta (OK de Noel 2026-07-02, documentado en el commit y en el
    runbook), producción bootea — la secuencia §5 sigue mandando operativamente."""
    monkeypatch.setenv("KALSHI_ENV", "production")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_2_SPORTSBOOK_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_F3_ACK", "NOEL-OK-F3")

    s = Settings()
    assert s.MOTOR_MM_EXECUTION_ENABLED and s.MOTOR_MM_F3_ACK == "NOEL-OK-F3"
    assert s.MOTOR_MM_MAX_EXPOSURE_USD == 100.0  # canary cap default


def test_motor_mm_execution_allowed_in_demo(fake_key: Path, monkeypatch):
    """F2: EXECUTION=true + ENABLED=true bootea contra DEMO (la validación de mecánica
    del plan §4 corre ahí; producción sigue bloqueada hasta F3)."""
    monkeypatch.setenv("KALSHI_ENV", "demo")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-id-12345")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(fake_key))
    monkeypatch.setenv("TRADING_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_ENABLED", "true")
    monkeypatch.setenv("MOTOR_MM_EXECUTION_ENABLED", "true")

    s = Settings()
    assert s.MOTOR_MM_EXECUTION_ENABLED and s.KALSHI_ENV == "demo"


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
