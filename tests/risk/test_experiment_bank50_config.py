"""Contrato Bank50 desarmado: configuración real, sin .env, credenciales ni red."""

import os
import socket
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.utils import config
from src.utils.config import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", {})

    def denied(*args, **kwargs):
        raise AssertionError("BANK50_CONFIG_NO_NETWORK")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
def minimum(tmp_path):
    # El validador de ruta sólo exige existencia. No es una clave, no se firma nada.
    placeholder = tmp_path / "not-a-private-key.pem"
    placeholder.write_text("offline test placeholder; not credential material")
    return {
        "_env_file": None,
        "KALSHI_API_KEY_ID": "bank50-offline-placeholder",
        "KALSHI_PRIVATE_KEY_PATH": placeholder,
        "DATABASE_URL": "sqlite:///:memory:",
    }


@pytest.fixture(params=["demo", "production"])
def profile(minimum, request):
    return {
        **minimum,
        "KALSHI_ENV": request.param,
        "EXPERIMENT_BANK50_ENABLED": True,
        "ACTIVE_CAPITAL_USD": 50,
        "CAPITAL_CAP_USD": 50,
        "CAPITAL_FLOOR_USD": 40.5,
        "CAPITAL_SAFETY_FACTOR_PCT": 90,
        "DYNAMIC_CAPITAL_ENABLED": True,
        "CAPITAL_SMOOTHING_PCT": 0,
        "MAX_TRADE_SIZE_USD": 1,
        "MAX_TRADE_SIZE_PCT": 3,
        "MAX_SIMULTANEOUS_EXPOSURE_PCT": 10,
        "MAX_EVENT_DIRECTIONAL_EXPOSURE_USD": 1,
        "BALANCE_MAX_AGE_SECONDS": 60,
        "EXPERIMENT_BALANCE_MAX_AGE_SEC": 60,
        "BALANCE_REFRESH_SECONDS": 30,
        "MAX_DAILY_LOSS_FLOOR_USD": 0,
        "MAX_WEEKLY_LOSS_FLOOR_USD": 0,
        "MAX_MONTHLY_LOSS_FLOOR_USD": 0,
        "DAILY_STOP_ENTRIES_ONLY": False,
        "EXPERIMENT_START_AT": "2000-01-01T00:00:00Z",
    }


def test_bank50_defaults_do_not_arm_or_change_legacy_limits(minimum):
    settings = Settings(**minimum)
    assert settings.EXPERIMENT_BANK50_ENABLED is False
    assert settings.EXPERIMENT_BANK200_ENABLED is False
    assert settings.TRADING_ENABLED is False
    assert settings.MOTOR_1_ARBITRAGE_ENABLED is False
    assert settings.MOTOR_1_EXECUTION_ENABLED is False
    assert settings.ACTIVE_CAPITAL_USD == 4000
    assert settings.CAPITAL_CAP_USD == 2000
    assert settings.CAPITAL_FLOOR_USD == 100
    assert settings.MAX_TRADE_SIZE_USD == 200
    assert settings.MAX_DAILY_LOSS_FLOOR_USD == 20
    assert settings.MAX_WEEKLY_LOSS_FLOOR_USD == 40
    assert settings.MAX_MONTHLY_LOSS_FLOOR_USD == 60
    assert settings.DAILY_STOP_ENTRIES_ONLY is True


def test_bank50_valid_profile_remains_unarmed(profile):
    settings = Settings(**profile)
    assert settings.EXPERIMENT_BANK50_ENABLED is True
    assert settings.TRADING_ENABLED is False
    assert settings.MOTOR_1_ARBITRAGE_ENABLED is False
    assert settings.MOTOR_1_EXECUTION_ENABLED is False
    assert settings.EXPERIMENT_BANK_CONFIRMED_USD == 0
    assert settings.CAPITAL_SAFETY_FACTOR_PCT == 90


@pytest.mark.parametrize(
    "field,value",
    [
        ("ACTIVE_CAPITAL_USD", 49),
        ("ACTIVE_CAPITAL_USD", 51),
        ("CAPITAL_CAP_USD", 49),
        ("CAPITAL_CAP_USD", 51),
        ("CAPITAL_FLOOR_USD", 40),
        ("CAPITAL_FLOOR_USD", 41),
        ("CAPITAL_SAFETY_FACTOR_PCT", 89),
        ("CAPITAL_SAFETY_FACTOR_PCT", 91),
        ("CAPITAL_SMOOTHING_PCT", 0.001),
        ("DYNAMIC_CAPITAL_ENABLED", False),
        ("MAX_TRADE_SIZE_USD", 1.01),
        ("MAX_TRADE_SIZE_PCT", 3.01),
        ("MAX_SIMULTANEOUS_EXPOSURE_PCT", 10.01),
        ("MAX_EVENT_DIRECTIONAL_EXPOSURE_USD", 1.01),
        ("BALANCE_MAX_AGE_SECONDS", 61),
        ("EXPERIMENT_BALANCE_MAX_AGE_SEC", 61),
        ("MAX_DAILY_LOSS_FLOOR_USD", 0.01),
        ("MAX_WEEKLY_LOSS_FLOOR_USD", 0.01),
        ("MAX_MONTHLY_LOSS_FLOOR_USD", 0.01),
        ("DAILY_STOP_ENTRIES_ONLY", True),
    ],
)
def test_bank50_rejects_every_unsafe_capital_or_budget_field_unarmed(profile, field, value):
    with pytest.raises(ValidationError, match=field):
        Settings(**{**profile, field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("MAX_TRADE_SIZE_USD", 0.5),
        ("MAX_TRADE_SIZE_PCT", 1),
        ("MAX_SIMULTANEOUS_EXPOSURE_PCT", 3),
        ("MAX_EVENT_DIRECTIONAL_EXPOSURE_USD", 0.5),
        ("BALANCE_MAX_AGE_SECONDS", 30),
        ("EXPERIMENT_BALANCE_MAX_AGE_SEC", 30),
    ],
)
def test_bank50_accepts_tighter_bounds_without_enabling_execution(profile, field, value):
    settings = Settings(**{**profile, field: value})
    assert getattr(settings, field) == value
    assert not settings.TRADING_ENABLED


def test_bank50_default_refresh_would_expire_capital_and_is_rejected(profile):
    values = dict(profile)
    del values["BALANCE_REFRESH_SECONDS"]
    with pytest.raises(ValidationError, match="BALANCE_REFRESH_SECONDS<=min"):
        Settings(**values)


@pytest.mark.parametrize("ttl_field", ["BALANCE_MAX_AGE_SECONDS", "EXPERIMENT_BALANCE_MAX_AGE_SEC"])
def test_bank50_refresh_cannot_exceed_either_ttl(profile, ttl_field):
    values = {**profile, ttl_field: 30, "BALANCE_REFRESH_SECONDS": 31}
    with pytest.raises(ValidationError, match="BALANCE_REFRESH_SECONDS<=min"):
        Settings(**values)
    assert Settings(**{**values, "BALANCE_REFRESH_SECONDS": 30})


def test_bank50_refresh_equal_to_both_ttls_is_valid(profile):
    assert Settings(**{**profile, "BALANCE_REFRESH_SECONDS": 60})


@pytest.mark.parametrize(
    "field",
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
def test_bank50_forbids_other_executors_and_position_managers_even_unarmed(profile, field):
    with pytest.raises(ValidationError, match=field):
        Settings(**{**profile, field: True})


def test_bank50_cannot_mix_bank200_even_in_demo(profile):
    with pytest.raises(ValidationError, match="EXPERIMENT_BANK200_ENABLED=false"):
        Settings(**{**profile, "EXPERIMENT_BANK200_ENABLED": True})


@pytest.mark.parametrize(
    "start",
    [
        "",
        "not-a-date",
        "2000-01-01",
        "2000-01-01T00:00:00",
        "2000-13-01T00:00:00Z",
        "9999-12-31T00:00:00+00:00",
    ],
)
def test_bank50_requires_valid_aware_nonfuture_start_even_unarmed(profile, start):
    with pytest.raises(ValidationError, match="EXPERIMENT_START_AT"):
        Settings(**{**profile, "EXPERIMENT_START_AT": start})


@pytest.mark.parametrize(
    "start", ["2000-01-01T00:00:00Z", "2000-01-01T00:00:00-07:00", "2000-01-01T00:00:00+14:00"]
)
def test_bank50_accepts_timezone_offsets(profile, start):
    assert start == Settings(**{**profile, "EXPERIMENT_START_AT": start}).EXPERIMENT_START_AT


def test_bank50_start_boundary_uses_aware_utc_instant(profile, monkeypatch):
    now = datetime(2026, 10, 7, 18, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return now

    monkeypatch.setattr(config, "datetime", FrozenDatetime)
    assert Settings(**{**profile, "EXPERIMENT_START_AT": now.isoformat()})
    with pytest.raises(ValidationError, match="futuro"):
        Settings(
            **{**profile, "EXPERIMENT_START_AT": (now + timedelta(microseconds=1)).isoformat()}
        )


def test_bank50_trading_requires_exact_ack_and_both_m1_gates(profile):
    with pytest.raises(ValidationError) as error:
        Settings(**{**profile, "TRADING_ENABLED": True})
    for name in (
        "EXPERIMENT_BANK_CONFIRMED_USD=50",
        "MOTOR_1_ARBITRAGE_ENABLED=true",
        "MOTOR_1_EXECUTION_ENABLED=true",
    ):
        assert name in str(error.value)


@pytest.mark.parametrize("ack", [0, 49, 51, 200])
def test_bank50_wrong_ack_is_rejected_even_with_m1_armed(profile, ack):
    with pytest.raises(ValidationError, match="EXPERIMENT_BANK_CONFIRMED_USD=50"):
        Settings(
            **{
                **profile,
                "TRADING_ENABLED": True,
                "EXPERIMENT_BANK_CONFIRMED_USD": ack,
                "MOTOR_1_ARBITRAGE_ENABLED": True,
                "MOTOR_1_EXECUTION_ENABLED": True,
            }
        )


@pytest.mark.parametrize("missing", ["MOTOR_1_ARBITRAGE_ENABLED", "MOTOR_1_EXECUTION_ENABLED"])
def test_bank50_ack_does_not_replace_a_required_m1_gate(profile, missing):
    armed = {
        **profile,
        "TRADING_ENABLED": True,
        "EXPERIMENT_BANK_CONFIRMED_USD": 50,
        "MOTOR_1_ARBITRAGE_ENABLED": True,
        "MOTOR_1_EXECUTION_ENABLED": True,
    }
    with pytest.raises(ValidationError, match=missing):
        Settings(**{**armed, missing: False})


def test_bank50_explicit_armed_configuration_only_validates_no_client_is_constructed(profile):
    settings = Settings(
        **{
            **profile,
            "TRADING_ENABLED": True,
            "EXPERIMENT_BANK_CONFIRMED_USD": 50,
            "MOTOR_1_ARBITRAGE_ENABLED": True,
            "MOTOR_1_EXECUTION_ENABLED": True,
        }
    )
    assert settings.TRADING_ENABLED and settings.EXPERIMENT_BANK50_ENABLED


def test_bank200_existing_unarmed_contract_stays_unchanged(minimum):
    legacy = {
        **minimum,
        "KALSHI_ENV": "production",
        "EXPERIMENT_BANK200_ENABLED": True,
        "ACTIVE_CAPITAL_USD": 200,
        "CAPITAL_CAP_USD": 200,
        "CAPITAL_FLOOR_USD": 180,
        "CAPITAL_SMOOTHING_PCT": 0,
        "MAX_TRADE_SIZE_USD": 2,
        "MAX_TRADE_SIZE_PCT": 1,
        "MAX_SIMULTANEOUS_EXPOSURE_PCT": 3,
        "MAX_EVENT_DIRECTIONAL_EXPOSURE_USD": 2,
    }
    settings = Settings(**legacy)
    assert not settings.EXPERIMENT_BANK50_ENABLED
    assert settings.EXPERIMENT_START_AT == ""  # Bank200 no exigía fecha desarmado.
    assert settings.MAX_DAILY_LOSS_FLOOR_USD == 20
    with pytest.raises(ValidationError, match="ACTIVE_CAPITAL_USD=200"):
        Settings(**{**legacy, "ACTIVE_CAPITAL_USD": 50})
