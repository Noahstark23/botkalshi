"""Preflight Bank50 local: entorno efectivo, salida saneada y unidad sin drift."""

from __future__ import annotations

import ast
import builtins
import io
import json
import os
import socket
import sqlite3
from pathlib import Path

import pytest

from scripts import check_bank50_deployment as check
from src.utils.config import Settings

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def environment(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("PREFLIGHT_CANNOT_CONNECT_NETWORK_OR_DATABASE")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(sqlite3, "connect", denied)
    monkeypatch.chdir(tmp_path)
    # No es una credencial: el validador de ruta comprueba existencia, no firma.
    key = tmp_path / "offline-not-a-key.pem"
    key.write_text("offline placeholder, not a private key")
    values = {
        "KALSHI_ENV": "production",
        "KALSHI_API_KEY_ID": "offline-key-id-not-secret",
        "KALSHI_PRIVATE_KEY_PATH": str(key),
        "DATABASE_URL": "sqlite:////var/lib/botkalshi-runtime/trades.db",
        "HEALTH_HOST": "127.0.0.1",
        "HEALTH_PORT": "18080",
        "EXPERIMENT_BANK50_ENABLED": "true",
        "EXPERIMENT_BANK200_ENABLED": "false",
        "EXPERIMENT_START_AT": "2000-01-01T00:00:00Z",
        "EXPERIMENT_BANK_CONFIRMED_USD": "0",
        "ACTIVE_CAPITAL_USD": "50",
        "CAPITAL_CAP_USD": "50",
        "CAPITAL_FLOOR_USD": "40.5",
        "CAPITAL_SAFETY_FACTOR_PCT": "90",
        "CAPITAL_SMOOTHING_PCT": "0",
        "BALANCE_REFRESH_SECONDS": "30",
        "BALANCE_MAX_AGE_SECONDS": "60",
        "EXPERIMENT_BALANCE_MAX_AGE_SEC": "60",
        "MAX_TRADE_SIZE_USD": "1",
        "MAX_TRADE_SIZE_PCT": "3",
        "MAX_SIMULTANEOUS_EXPOSURE_PCT": "10",
        "MAX_EVENT_DIRECTIONAL_EXPOSURE_USD": "1",
        "MAX_DAILY_LOSS_FLOOR_USD": "0",
        "MAX_WEEKLY_LOSS_FLOOR_USD": "0",
        "MAX_MONTHLY_LOSS_FLOOR_USD": "0",
        "DAILY_STOP_ENTRIES_ONLY": "false",
        "TRADING_ENABLED": "false",
        "MOTOR_1_ARBITRAGE_ENABLED": "false",
        "MOTOR_1_EXECUTION_ENABLED": "false",
        "MOTOR_2_SPORTSBOOK_ENABLED": "false",
        "MOTOR_2_ENTRY_EXECUTION_ENABLED": "false",
        "MOTOR_2_EXECUTION_ENABLED": "false",
        "MOTOR_2_TAKE_PROFIT_ENABLED": "false",
        "MOTOR_2_TRAILING_ENABLED": "false",
        "MOTOR_3_CLV_ENABLED": "false",
        "MOTOR_3_EXECUTION_ENABLED": "false",
        "MOTOR_3_TAKE_PROFIT_ENABLED": "false",
        "MOTOR_3_TRAILING_ENABLED": "false",
        "MOTOR_3_MANAGES_ORPHANS": "false",
        "MOTOR_REST_ENABLED": "false",
        "MOTOR_REST_EXECUTION_ENABLED": "false",
        "MOTOR_MM_ENABLED": "false",
        "MOTOR_MM_EXECUTION_ENABLED": "false",
        "MOTOR_6_LINEMOVE_ENABLED": "false",
        "MOTOR_8_OFI_ENABLED": "false",
        "MOTOR_9_SPILLOVER_ENABLED": "false",
        "ANALYST_LOOP_ENABLED": "false",
        "DAILY_PNL_REPORT_ENABLED": "false",
        "TELEGRAM_DASHBOARD_ENABLED": "false",
        "MEMORY_MONITOR_ENABLED": "false",
        "PERSIST_ORDERBOOK_EVENTS": "false",
        "DB_MAINTENANCE_ENABLED": "true",
        "DISK_GUARD_ENABLED": "true",
        "ODDS_API_KEY": "",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
    }
    monkeypatch.setattr(os, "environ", values)
    return values


def report(capsys):
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def test_unarmed_effective_environment_passes_without_db_network_or_autoarming(environment, capsys):
    before = dict(environment)
    assert check.main() == 0
    result = report(capsys)
    assert result == {
        "check": "bank50_deployment",
        "scope": "local_settings_only",
        "status": "PASS",
        "mode": "unarmed",
    }
    assert environment == before


def test_constructor_explicitly_disables_dotenv_and_does_not_use_singleton(
    environment, capsys, monkeypatch
):
    calls = []

    def fresh_settings(**kwargs):
        calls.append(kwargs)
        return Settings(**kwargs)

    monkeypatch.setattr(check, "Settings", fresh_settings)
    monkeypatch.setattr("src.utils.config.get_settings", lambda: pytest.fail("singleton forbidden"))
    assert check.main() == 0
    assert calls == [{"_env_file": None}]
    assert report(capsys)["mode"] == "unarmed"


@pytest.mark.parametrize(
    "kind", ["file", "unreadable", "unreadable_symlink", "broken_symlink", "directory"]
)
def test_rejects_dotenv_in_working_directory_before_settings_without_reading_it(
    environment, capsys, monkeypatch, tmp_path, kind
):
    dotenv = tmp_path / ".env"
    if kind == "broken_symlink":
        dotenv.symlink_to(tmp_path / "missing-target")
        assert not dotenv.exists()
    elif kind == "directory":
        dotenv.mkdir()
    else:
        target = tmp_path / "unreadable-target" if kind == "unreadable_symlink" else dotenv
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, b"SECRET_DOTENV_CONTENT_MUST_NOT_BE_READ")
        finally:
            os.close(descriptor)
        if kind in {"unreadable", "unreadable_symlink"}:
            target.chmod(0)
        if kind == "unreadable_symlink":
            dotenv.symlink_to(target)

    def forbid_dotenv_read(original):
        def guarded(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and Path(file).name == ".env":
                pytest.fail("dotenv must not be opened")
            return original(file, *args, **kwargs)

        return guarded

    monkeypatch.setattr(builtins, "open", forbid_dotenv_read(builtins.open))
    monkeypatch.setattr(io, "open", forbid_dotenv_read(io.open))
    monkeypatch.setattr(check, "Settings", lambda **_: pytest.fail("Settings must not be created"))
    assert check.main() == 2
    assert report(capsys) == {
        "check": "bank50_deployment",
        "scope": "local_settings_only",
        "status": "FAIL",
        "reason": "LOCAL_DOTENV_NOT_ALLOWED",
        "fields": [],
    }


def test_m1_study_is_optional_and_does_not_arm_orders(environment, capsys):
    environment["MOTOR_1_ARBITRAGE_ENABLED"] = "true"
    assert check.main() == 0
    assert report(capsys)["mode"] == "unarmed"
    assert environment["MOTOR_1_EXECUTION_ENABLED"] == "false"


@pytest.mark.parametrize(
    "field,value",
    [
        ("EXPERIMENT_BANK50_ENABLED", "false"),
        ("EXPERIMENT_BANK200_ENABLED", "true"),
        ("KALSHI_ENV", "demo"),
        ("DATABASE_URL", "sqlite:////tmp/wrong-history.db"),
        ("DATABASE_URL", "sqlite:////var/lib/botkalshi-runtime/trades.db?mode=ro"),
        ("HEALTH_HOST", "0.0.0.0"),
        ("HEALTH_HOST", "localhost"),
        ("HEALTH_PORT", "8080"),
        ("KALSHI_PROD_REST_URL", "https://unapproved.invalid/trade-api/v2"),
        ("KALSHI_PROD_REST_URL", "https://api.elections.kalshi.com/trade-api/v2/"),
        ("KALSHI_PROD_WS_URL", "wss://unapproved.invalid/trade-api/ws/v2"),
        ("KALSHI_PROD_WS_URL", "ws://api.elections.kalshi.com/trade-api/ws/v2"),
        ("DB_MAINTENANCE_ENABLED", "false"),
        ("DISK_GUARD_ENABLED", "false"),
    ],
)
def test_deployment_rejects_profile_identity_paths_hosts_and_resources(
    environment, capsys, field, value
):
    environment[field] = value
    assert check.main() == 2
    assert report(capsys)["status"] == "FAIL"


@pytest.mark.parametrize(
    "flag",
    [
        "MOTOR_2_SPORTSBOOK_ENABLED",
        "MOTOR_2_ENTRY_EXECUTION_ENABLED",
        "MOTOR_2_EXECUTION_ENABLED",
        "MOTOR_2_TAKE_PROFIT_ENABLED",
        "MOTOR_2_TRAILING_ENABLED",
        "MOTOR_3_CLV_ENABLED",
        "MOTOR_3_EXECUTION_ENABLED",
        "MOTOR_3_TAKE_PROFIT_ENABLED",
        "MOTOR_3_TRAILING_ENABLED",
        "MOTOR_3_MANAGES_ORPHANS",
        "MOTOR_REST_ENABLED",
        "MOTOR_REST_EXECUTION_ENABLED",
        "MOTOR_MM_ENABLED",
        "MOTOR_MM_EXECUTION_ENABLED",
        "MOTOR_6_LINEMOVE_ENABLED",
        "MOTOR_8_OFI_ENABLED",
        "MOTOR_9_SPILLOVER_ENABLED",
        "ANALYST_LOOP_ENABLED",
        "DAILY_PNL_REPORT_ENABLED",
        "TELEGRAM_DASHBOARD_ENABLED",
        "MEMORY_MONITOR_ENABLED",
        "PERSIST_ORDERBOOK_EVENTS",
    ],
)
def test_every_unnecessary_cycle_executor_or_manager_is_rejected(environment, capsys, flag):
    environment[flag] = "true"
    assert check.main() == 2
    assert report(capsys)["status"] == "FAIL"


@pytest.mark.parametrize("field", ["ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
def test_each_external_service_secret_is_rejected_individually_without_echo(
    environment, capsys, field
):
    environment[field] = "SECRET_VALUE_MUST_NEVER_APPEAR"
    assert check.main() == 2
    output = capsys.readouterr()
    assert "SECRET_VALUE_MUST_NEVER_APPEAR" not in output.out + output.err
    assert field in json.loads(output.out)["fields"]


def test_unarmed_ack_must_be_zero(environment, capsys):
    environment["EXPERIMENT_BANK_CONFIRMED_USD"] = "50"
    assert check.main() == 2
    assert "EXPERIMENT_BANK_CONFIRMED_USD" in report(capsys)["fields"]


def test_unarmed_rejects_m1_executor_prearming(environment, capsys):
    environment.update(MOTOR_1_ARBITRAGE_ENABLED="true", MOTOR_1_EXECUTION_ENABLED="true")
    assert check.main() == 2
    assert "MOTOR_1_EXECUTION_ENABLED" in report(capsys)["fields"]


def test_future_explicit_armed_m1_only_configuration_is_validated_without_mutation(
    environment, capsys
):
    environment.update(
        TRADING_ENABLED="true",
        EXPERIMENT_BANK_CONFIRMED_USD="50",
        MOTOR_1_ARBITRAGE_ENABLED="true",
        MOTOR_1_EXECUTION_ENABLED="true",
    )
    before = dict(environment)
    assert check.main() == 0
    assert report(capsys)["mode"] == "armed"
    assert environment == before


@pytest.mark.parametrize(
    "missing",
    ["EXPERIMENT_BANK_CONFIRMED_USD", "MOTOR_1_ARBITRAGE_ENABLED", "MOTOR_1_EXECUTION_ENABLED"],
)
def test_armed_requires_bank50_ack_and_both_m1_gates(environment, capsys, missing):
    environment.update(
        TRADING_ENABLED="true",
        EXPERIMENT_BANK_CONFIRMED_USD="50",
        MOTOR_1_ARBITRAGE_ENABLED="true",
        MOTOR_1_EXECUTION_ENABLED="true",
    )
    environment[missing] = "0" if missing == "EXPERIMENT_BANK_CONFIRMED_USD" else "false"
    assert check.main() == 2
    assert report(capsys)["reason"] == "SETTINGS_INVALID"


@pytest.mark.parametrize(
    "field,value",
    [
        ("KALSHI_PRIVATE_KEY", "SECRET_INVALID_PEM"),
        ("HEALTH_PORT", "SECRET_INVALID_INTEGER"),
        ("EXPERIMENT_START_AT", "SECRET_INVALID_TIMESTAMP"),
        ("DATABASE_URL", "SECRET_WRONG_DATABASE"),
    ],
)
def test_pydantic_and_profile_failures_never_print_input(environment, capsys, field, value):
    environment[field] = value
    assert check.main() == 2
    output = capsys.readouterr()
    assert value not in output.out + output.err
    assert "input_value" not in output.out + output.err
    assert "Traceback" not in output.out + output.err
    assert json.loads(output.out)["status"] == "FAIL"


def test_preflight_has_no_db_client_runner_or_network_imports():
    tree = ast.parse((ROOT / "scripts/check_bank50_deployment.py").read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module)
    assert set(imports) <= {"__future__", "json", "os", "pydantic", "src.utils.config"}


def test_unit_preserves_every_existing_operational_and_hardening_directive():
    baseline = (ROOT / "infra/digitalocean-runtime/botkalshi-runtime.service").read_text()
    candidate = (ROOT / "infra/pilot-bank50/botkalshi-runtime.service").read_text()

    def directives(text):
        return [
            line
            for line in text.splitlines()
            if line
            and not line.startswith("#")
            and not line.startswith(("Description=", "ExecStartPre="))
        ]

    assert directives(candidate) == directives(baseline)
    assert candidate.count("ExecStartPre=") == 1
    assert (
        "ExecStartPre=/opt/botkalshi-runtime/current/.venv/bin/python "
        "-m scripts.check_bank50_deployment"
    ) in candidate
    assert "STANDBY_REVIEWED" in candidate and "Nombre legado" in candidate
    assert "TRADING_ENABLED=true" not in candidate
    assert "NoNewPrivileges=true" in candidate and "MemoryMax=650M" in candidate
