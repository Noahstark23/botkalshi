"""El perfil de riesgo debe llegar a Settings también al desplegar con Compose.

Guard contractual sobre archivos: no arranca servicios, no usa claves y no consulta
Kalshi. Evita repetir el fallo de añadir un guard a Python sin inyectarlo al contenedor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.utils.config import Settings

ROOT = Path(__file__).resolve().parents[1]
RISK_FIELDS = (
    "MAX_TRADE_SIZE_USD",
    "MAX_EVENT_DIRECTIONAL_EXPOSURE_USD",
    "DYNAMIC_CAPITAL_ENABLED",
    "CAPITAL_SAFETY_FACTOR_PCT",
    "CAPITAL_FLOOR_USD",
    "CAPITAL_CAP_USD",
    "CAPITAL_SMOOTHING_PCT",
    "BALANCE_REFRESH_SECONDS",
    "EXPERIMENT_BANK200_ENABLED",
    "EXPERIMENT_START_AT",
    "EXPERIMENT_BALANCE_MAX_AGE_SEC",
    "EXPERIMENT_BANK_CONFIRMED_USD",
    "MOTOR_3_MANAGES_ORPHANS",
)
PROFILE_FIELDS = RISK_FIELDS + (
    "KALSHI_ENV",
    "ACTIVE_CAPITAL_USD",
    "MAX_TRADE_SIZE_PCT",
    "MAX_SIMULTANEOUS_EXPOSURE_PCT",
    "TRADING_ENABLED",
    "MOTOR_1_ARBITRAGE_ENABLED",
    "MOTOR_1_EXECUTION_ENABLED",
    "MOTOR_3_CLV_ENABLED",
    "MOTOR_3_EXECUTION_ENABLED",
    "MOTOR_2_ENTRY_EXECUTION_ENABLED",
    "MOTOR_REST_EXECUTION_ENABLED",
    "MOTOR_MM_EXECUTION_ENABLED",
)


def _compose_defaults() -> dict[str, str]:
    text = (ROOT / "docker-compose.yml").read_text()
    # El contrato exige passthrough del mismo nombre, no un literal que ignore al host.
    block = text.split("    environment:\n", 1)[1].split("    volumes:\n", 1)[0]
    pairs = re.findall(r"^      ([A-Z][A-Z0-9_]*): \$\{\1:-([^}]*)\}$", block, re.M)
    assert len(pairs) == len(dict(pairs)), "Variables duplicadas en Compose"
    return dict(pairs)


def _example_defaults() -> dict[str, str]:
    text = (ROOT / ".env.example").read_text()
    pairs = re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.M)
    assert len(pairs) == len(dict(pairs)), "Variables duplicadas en .env.example"
    return dict(pairs)


@pytest.mark.parametrize("name", PROFILE_FIELDS)
def test_bank200_profile_is_forwarded_with_same_name(name: str) -> None:
    assert name in _compose_defaults(), f"{name} no llega al proceso vía Compose"
    assert name in _example_defaults(), f"{name} falta en .env.example"


@pytest.mark.parametrize("name", RISK_FIELDS)
def test_new_wiring_preserves_settings_defaults(name: str) -> None:
    default = Settings.model_fields[name].default
    expected = str(default).lower() if isinstance(default, bool) else str(default)
    assert _compose_defaults()[name] == expected
    assert _example_defaults()[name] == expected


def test_deployment_does_not_enable_execution_or_acknowledge_capital() -> None:
    for defaults in (_compose_defaults(), _example_defaults()):
        for name, value in defaults.items():
            if name.endswith("_EXECUTION_ENABLED") or name in {
                "TRADING_ENABLED",
                "EXPERIMENT_BANK200_ENABLED",
            }:
                assert value == "false", f"{name} debe seguir opt-in"
        assert defaults["EXPERIMENT_START_AT"] == ""
        assert float(defaults["EXPERIMENT_BANK_CONFIRMED_USD"]) == 0
