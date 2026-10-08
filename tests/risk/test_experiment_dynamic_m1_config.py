"""Perfil dinámico explícito: sin credenciales reales, .env, DB ni red."""

import os
import socket

import pytest
from pydantic import ValidationError

from src.utils.config import Settings


@pytest.fixture(params=["demo", "production"])
def profile(tmp_path, monkeypatch, request):
    monkeypatch.setattr(os, "environ", {})

    def denied(*args, **kwargs):
        raise AssertionError("DYNAMIC_M1_CONFIG_NO_NETWORK")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    key = tmp_path / "not-a-key.pem"
    key.write_text("offline placeholder, not credential material")
    return {
        "_env_file": None,
        "KALSHI_ENV": request.param,
        "KALSHI_API_KEY_ID": "offline-placeholder",
        "KALSHI_PRIVATE_KEY_PATH": key,
        "EXPERIMENT_DYNAMIC_M1_ENABLED": True,
        "EXPERIMENT_START_AT": "2000-01-01T00:00:00Z",
        "ACTIVE_CAPITAL_USD": 100.43,
        "CAPITAL_CAP_USD": 5000,
        "CAPITAL_FLOOR_USD": 40.5,
        "CAPITAL_SAFETY_FACTOR_PCT": 90,
        "CAPITAL_SMOOTHING_PCT": 0,
        "BALANCE_REFRESH_SECONDS": 30,
        "BALANCE_MAX_AGE_SECONDS": 60,
        "EXPERIMENT_BALANCE_MAX_AGE_SEC": 60,
        "MAX_TRADE_SIZE_USD": 1,
        "MAX_TRADE_SIZE_PCT": 3,
        "MAX_SIMULTANEOUS_EXPOSURE_PCT": 10,
        "MAX_EVENT_DIRECTIONAL_EXPOSURE_USD": 1,
        "MAX_DAILY_LOSS_FLOOR_USD": 0,
        "MAX_WEEKLY_LOSS_FLOOR_USD": 0,
        "MAX_MONTHLY_LOSS_FLOOR_USD": 0,
        "DAILY_STOP_ENTRIES_ONLY": False,
    }


def test_dynamic_profile_remains_opt_in_and_unarmed(profile):
    settings = Settings(**profile)
    assert settings.EXPERIMENT_DYNAMIC_M1_ENABLED
    assert not settings.EXPERIMENT_BANK50_ENABLED and not settings.EXPERIMENT_BANK200_ENABLED
    assert not settings.TRADING_ENABLED and not settings.MOTOR_1_EXECUTION_ENABLED
    assert Settings(**{**profile, "EXPERIMENT_DYNAMIC_M1_ENABLED": False})
    assert Settings.model_fields["EXPERIMENT_DYNAMIC_M1_ENABLED"].default is False


@pytest.mark.parametrize("reference", [1, 49.49, 100.43, 4000])
@pytest.mark.parametrize("legacy_ack", [0, 49.49, 100.43])
def test_reference_and_legacy_ack_are_not_a_fixed_bank(profile, reference, legacy_ack):
    settings = Settings(
        **{**profile, "ACTIVE_CAPITAL_USD": reference, "EXPERIMENT_BANK_CONFIRMED_USD": legacy_ack}
    )
    assert reference == settings.ACTIVE_CAPITAL_USD
    armed = Settings(
        **{
            **profile,
            "TRADING_ENABLED": True,
            "MOTOR_1_ARBITRAGE_ENABLED": True,
            "MOTOR_1_EXECUTION_ENABLED": True,
            "EXPERIMENT_BANK_CONFIRMED_USD": legacy_ack,
        }
    )
    assert armed.TRADING_ENABLED  # Valida un objeto; no construye clientes ni ejecutores.


@pytest.mark.parametrize("bank", ["EXPERIMENT_BANK50_ENABLED", "EXPERIMENT_BANK200_ENABLED"])
def test_dynamic_cannot_mix_other_profiles_even_in_demo(profile, bank):
    with pytest.raises(ValidationError):
        Settings(**{**profile, bank: True})


@pytest.mark.parametrize(
    "field,value",
    [
        ("DYNAMIC_CAPITAL_ENABLED", False),
        ("CAPITAL_CAP_USD", 5001),
        ("CAPITAL_CAP_USD", float("inf")),
        ("CAPITAL_CAP_USD", float("nan")),
        ("CAPITAL_CAP_USD", 40),
        ("CAPITAL_FLOOR_USD", float("inf")),
        ("CAPITAL_FLOOR_USD", 0),
        ("CAPITAL_FLOOR_USD", 5001),
        ("CAPITAL_SAFETY_FACTOR_PCT", 91),
        ("CAPITAL_SMOOTHING_PCT", 0.05),
        ("BALANCE_REFRESH_SECONDS", 60),
        ("BALANCE_MAX_AGE_SECONDS", 61),
        ("EXPERIMENT_BALANCE_MAX_AGE_SEC", 61),
        ("MAX_TRADE_SIZE_USD", 1.01),
        ("MAX_TRADE_SIZE_PCT", 3.01),
        ("MAX_SIMULTANEOUS_EXPOSURE_PCT", 10.01),
        ("MAX_EVENT_DIRECTIONAL_EXPOSURE_USD", 1.01),
        ("MAX_DAILY_LOSS_FLOOR_USD", 0.01),
        ("MAX_WEEKLY_LOSS_FLOOR_USD", 0.01),
        ("MAX_MONTHLY_LOSS_FLOOR_USD", 0.01),
        ("DAILY_STOP_ENTRIES_ONLY", True),
        ("EXPERIMENT_START_AT", ""),
        ("EXPERIMENT_START_AT", "2026-01-01"),
        ("EXPERIMENT_START_AT", "9999-01-01T00:00:00Z"),
    ],
)
def test_dynamic_rejects_invalid_or_weakened_profile(profile, field, value):
    with pytest.raises(ValidationError, match=field):
        Settings(**{**profile, field: value})


@pytest.mark.parametrize(
    "flag",
    [
        "MOTOR_2_ENTRY_EXECUTION_ENABLED",
        "MOTOR_2_EXECUTION_ENABLED",
        "MOTOR_REST_EXECUTION_ENABLED",
        "MOTOR_MM_EXECUTION_ENABLED",
        "MOTOR_3_CLV_ENABLED",
        "MOTOR_3_EXECUTION_ENABLED",
        "MOTOR_3_MANAGES_ORPHANS",
    ],
)
def test_dynamic_rejects_other_position_managers(profile, flag):
    with pytest.raises(ValidationError, match=flag):
        Settings(**{**profile, flag: True})


@pytest.mark.parametrize("missing", ["MOTOR_1_ARBITRAGE_ENABLED", "MOTOR_1_EXECUTION_ENABLED"])
def test_trading_still_requires_both_m1_gates(profile, missing):
    values = {
        **profile,
        "TRADING_ENABLED": True,
        "MOTOR_1_ARBITRAGE_ENABLED": True,
        "MOTOR_1_EXECUTION_ENABLED": True,
        missing: False,
    }
    with pytest.raises(ValidationError, match=missing):
        Settings(**values)
