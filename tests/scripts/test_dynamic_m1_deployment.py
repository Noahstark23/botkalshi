"""Contrato local del candidato M1 dinámico, sin acceso financiero ni despliegue."""

from __future__ import annotations

import ast
import builtins
import io
import json
import os
from pathlib import Path

import pytest

from scripts import check_bank50_deployment as bank50
from scripts import check_dynamic_m1_deployment as check
from src.utils.config import Settings
from tests.scripts.test_bank50_deployment import environment as bank50_environment  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def environment(bank50_environment):  # noqa: F811 - dependencia de fixture importada
    # La fixture compartida niega sockets/DNS/SQLite y usa cwd/PEM ficticio temporales.
    values = bank50_environment
    values.update(
        EXPERIMENT_DYNAMIC_M1_ENABLED="true",
        EXPERIMENT_BANK50_ENABLED="false",
        EXPERIMENT_BANK200_ENABLED="false",
        ACTIVE_CAPITAL_USD="100.43",
        CAPITAL_CAP_USD="5000",
    )
    return values


def report(capsys):
    output = capsys.readouterr()
    assert not output.err
    return json.loads(output.out)


@pytest.mark.parametrize("reference", ["49.49", "100.43"])
@pytest.mark.parametrize("legacy_ack", ["0", "49.49", "100.43"])
@pytest.mark.parametrize("armed", [False, True])
def test_reference_and_legacy_ack_never_fix_cash_or_arm_flags(
    environment, capsys, reference, legacy_ack, armed
):
    environment.update(ACTIVE_CAPITAL_USD=reference, EXPERIMENT_BANK_CONFIRMED_USD=legacy_ack)
    if armed:
        environment.update(
            TRADING_ENABLED="true",
            MOTOR_1_ARBITRAGE_ENABLED="true",
            MOTOR_1_EXECUTION_ENABLED="true",
        )
    before = dict(environment)
    assert check.main() == 0
    assert report(capsys) == {
        "check": "dynamic_m1_deployment",
        "scope": "local_settings_only",
        "status": "PASS",
        "mode": "armed" if armed else "unarmed",
    }
    assert environment == before


def test_uses_real_dynamic_settings_without_dotenv_or_singleton(environment, capsys, monkeypatch):
    calls = []

    def fresh_settings(**kwargs):
        calls.append(kwargs)
        settings = Settings(**kwargs)
        assert settings.EXPERIMENT_DYNAMIC_M1_ENABLED is True
        assert settings.CAPITAL_CAP_USD == 5000
        return settings

    monkeypatch.setattr(check, "Settings", fresh_settings)
    monkeypatch.setattr("src.utils.config.get_settings", lambda: pytest.fail("singleton forbidden"))
    assert check.main() == 0
    assert calls == [{"_env_file": None}]
    assert report(capsys)["mode"] == "unarmed"


@pytest.mark.parametrize(
    "field,value",
    [
        ("EXPERIMENT_DYNAMIC_M1_ENABLED", "false"),
        ("EXPERIMENT_BANK50_ENABLED", "true"),
        ("EXPERIMENT_BANK200_ENABLED", "true"),
        ("DYNAMIC_CAPITAL_ENABLED", "false"),
        ("KALSHI_ENV", "demo"),
        ("DATABASE_URL", "sqlite:////tmp/other.db"),
        ("DATABASE_URL", "sqlite:////var/lib/botkalshi-runtime/trades.db?mode=ro"),
        ("KALSHI_PROD_REST_URL", "https://other.invalid/trade-api/v2"),
        ("KALSHI_PROD_REST_URL", bank50.REST_URL + "/"),
        ("KALSHI_PROD_WS_URL", "wss://other.invalid/trade-api/ws/v2"),
        ("HEALTH_HOST", "0.0.0.0"),
        ("HEALTH_PORT", "8080"),
        ("MAX_TRADE_SIZE_USD", "1.01"),
        ("MAX_EVENT_DIRECTIONAL_EXPOSURE_USD", "1.01"),
        ("BALANCE_MAX_AGE_SECONDS", "61"),
        ("BALANCE_REFRESH_SECONDS", "61"),
        ("CAPITAL_SAFETY_FACTOR_PCT", "100"),
        ("CAPITAL_CAP_USD", "5001"),
        ("DB_MAINTENANCE_ENABLED", "false"),
        ("DISK_GUARD_ENABLED", "false"),
    ],
)
def test_rejects_wrong_profile_environment_and_independent_limits(
    environment, capsys, field, value
):
    environment[field] = value
    assert check.main() == 2
    assert report(capsys)["status"] == "FAIL"


@pytest.mark.parametrize("flag", bank50.DISABLED_FLAGS)
def test_other_cycles_managers_and_services_remain_disabled(environment, capsys, flag):
    environment[flag] = "true"
    assert check.main() == 2
    assert report(capsys)["status"] == "FAIL"


@pytest.mark.parametrize("field", bank50.EMPTY_FIELDS)
def test_rejects_external_credentials_without_echo(environment, capsys, field):
    environment[field] = "SECRET_EXTERNAL_VALUE"
    assert check.main() == 2
    output = capsys.readouterr()
    assert "SECRET_EXTERNAL_VALUE" not in output.out + output.err
    assert field in json.loads(output.out)["fields"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("KALSHI_PRIVATE_KEY", "SECRET_PEM_INPUT"),
        ("HEALTH_PORT", "SECRET_INTEGER_INPUT"),
        ("EXPERIMENT_START_AT", "SECRET_DATE_INPUT"),
    ],
)
def test_validation_errors_never_echo_inputs(environment, capsys, field, value):
    environment[field] = value
    assert check.main() == 2
    output = capsys.readouterr()
    assert value not in output.out + output.err
    assert "input_value" not in output.out + output.err
    assert "Traceback" not in output.out + output.err
    assert json.loads(output.out)["reason"] == "SETTINGS_INVALID"


def test_shadow_is_valid_but_partial_execution_is_rejected(environment, capsys):
    environment["MOTOR_1_ARBITRAGE_ENABLED"] = "true"
    assert check.main() == 0
    assert report(capsys)["mode"] == "unarmed"
    environment["MOTOR_1_EXECUTION_ENABLED"] = "true"
    assert check.main() == 2
    assert "MOTOR_1_EXECUTION_ENABLED" in report(capsys)["fields"]


@pytest.mark.parametrize("missing", ["MOTOR_1_ARBITRAGE_ENABLED", "MOTOR_1_EXECUTION_ENABLED"])
def test_trading_requires_both_m1_flags(environment, capsys, missing):
    environment.update(
        TRADING_ENABLED="true", MOTOR_1_ARBITRAGE_ENABLED="true", MOTOR_1_EXECUTION_ENABLED="true"
    )
    environment[missing] = "false"
    assert check.main() == 2
    assert report(capsys)["status"] == "FAIL"


@pytest.mark.parametrize("kind", ["file", "unreadable", "broken_symlink", "directory"])
def test_dotenv_in_cwd_rejected_without_open_or_settings(
    environment, capsys, tmp_path, monkeypatch, kind
):
    dotenv = tmp_path / ".env"
    if kind == "broken_symlink":
        dotenv.symlink_to(tmp_path / "missing-target")
    elif kind == "directory":
        dotenv.mkdir()
    else:
        descriptor = os.open(dotenv, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        if kind == "unreadable":
            dotenv.chmod(0)

    def guarded(original):
        def read(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)) and Path(file).name == ".env":
                pytest.fail("dotenv read forbidden")
            return original(file, *args, **kwargs)

        return read

    monkeypatch.setattr(builtins, "open", guarded(builtins.open))
    monkeypatch.setattr(io, "open", guarded(io.open))
    monkeypatch.setattr(check, "Settings", lambda **_: pytest.fail("Settings forbidden"))
    assert check.main() == 2
    assert report(capsys)["reason"] == "LOCAL_DOTENV_NOT_ALLOWED"


def test_local_example_has_no_credentials_and_no_enabled_trading_cycle(environment, capsys):
    lines = (ROOT / "infra/dynamic-m1/review.env.example").read_text().splitlines()
    values = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    assert "KALSHI_API_KEY_ID" not in values and "KALSHI_PRIVATE_KEY" not in values
    assert values["EXPERIMENT_START_AT"] == ""
    assert values["EXPERIMENT_BANK_CONFIRMED_USD"] == "0"
    assert values["ACTIVE_CAPITAL_USD"] == "100.43"
    assert values["CAPITAL_CAP_USD"] == "5000"
    assert values["CAPITAL_FLOOR_USD"] == "40.5"
    for field in ("TRADING_ENABLED", "MOTOR_1_ARBITRAGE_ENABLED", "MOTOR_1_EXECUTION_ENABLED"):
        assert values[field] == "false"
    assert all(values[field] == "false" for field in bank50.DISABLED_FLAGS)
    assert all(values[field] == "" for field in bank50.EMPTY_FIELDS)
    environment.update(values)
    assert check.main() == 2  # No inventar un inicio para hacer arrancable la plantilla.
    assert report(capsys)["reason"] == "SETTINGS_INVALID"
    environment["EXPERIMENT_START_AT"] = "2000-01-01T00:00:00Z"
    assert check.main() == 0
    assert report(capsys)["mode"] == "unarmed"


def test_unit_preserves_all_operational_directives_and_legacy_marker():
    def directives(path):
        return [
            line
            for line in path.read_text().splitlines()
            if line
            and not line.startswith("#")
            and not line.startswith(("Description=", "ExecStartPre="))
        ]

    candidate = ROOT / "infra/dynamic-m1/botkalshi-runtime.service"
    assert directives(candidate) == directives(
        ROOT / "infra/digitalocean-runtime/botkalshi-runtime.service"
    )
    text = candidate.read_text()
    assert text.count("ExecStartPre=") == 1
    assert "-m scripts.check_dynamic_m1_deployment" in text
    assert "STANDBY_REVIEWED" in text and "Nombre legado" in text
    assert "LOCAL NO instalado" in text and "TRADING_ENABLED=true" not in text


def test_preflight_shares_only_constants_and_has_no_financial_dependencies():
    tree = ast.parse((ROOT / "scripts/check_dynamic_m1_deployment.py").read_text())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports <= {
        "__future__",
        "json",
        "os",
        "pydantic",
        "src.utils.config",
        "scripts.check_bank50_deployment",
    }
    assert check.DISABLED_FLAGS is bank50.DISABLED_FLAGS
    assert check.REQUIRED_FLAGS is bank50.REQUIRED_FLAGS
    assert check.EMPTY_FIELDS is bank50.EMPTY_FIELDS
