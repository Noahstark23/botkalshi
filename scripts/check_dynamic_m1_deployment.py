"""Preflight local del perfil M1 dinámico; no instala ni habilita ejecución.

Lee Settings(_env_file=None) del entorno efectivo y rechaza .env en cwd sin
abrirlo, pues el runner podría cargarlo. Settings puede comprobar la existencia
del PEM local o validar material entregado por entorno. No consulta DB ni red,
no lee saldo y no cambia flags. PASS acredita sólo configuración local.
Uso desde WorkingDirectory: python -m scripts.check_dynamic_m1_deployment
"""

from __future__ import annotations

import json
import os

from pydantic import ValidationError

from scripts.check_bank50_deployment import (
    DATABASE_URL,
    DISABLED_FLAGS,
    EMPTY_FIELDS,
    REQUIRED_FLAGS,
    REST_URL,
    WS_URL,
)
from src.utils.config import Settings


def blockers(settings: Settings) -> list[str]:
    """Nombres constantes de campos inválidos, sin valores ni secretos."""
    exact = {
        "EXPERIMENT_DYNAMIC_M1_ENABLED": True,
        "EXPERIMENT_BANK50_ENABLED": False,
        "EXPERIMENT_BANK200_ENABLED": False,
        "DYNAMIC_CAPITAL_ENABLED": True,
        "KALSHI_ENV": "production",
        "DATABASE_URL": DATABASE_URL,
        "KALSHI_PROD_REST_URL": REST_URL,
        "KALSHI_PROD_WS_URL": WS_URL,
        "HEALTH_HOST": "127.0.0.1",
        "HEALTH_PORT": 18080,
    }
    invalid = [name for name, expected in exact.items() if getattr(settings, name) != expected]
    if settings.rest_url != REST_URL:
        invalid.append("KALSHI_PROD_REST_URL")
    if settings.ws_url != WS_URL:
        invalid.append("KALSHI_PROD_WS_URL")
    invalid.extend(name for name in DISABLED_FLAGS if getattr(settings, name))
    invalid.extend(name for name in REQUIRED_FLAGS if not getattr(settings, name))
    invalid.extend(name for name in EMPTY_FIELDS if getattr(settings, name) != "")
    if settings.TRADING_ENABLED:
        for name in ("MOTOR_1_ARBITRAGE_ENABLED", "MOTOR_1_EXECUTION_ENABLED"):
            if not getattr(settings, name):
                invalid.append(name)
    elif settings.MOTOR_1_EXECUTION_ENABLED:
        invalid.append("MOTOR_1_EXECUTION_ENABLED")
    # ACTIVE_CAPITAL_USD es referencia; el ack de bancos fijos se ignora aquí.
    # Settings valida la política dinámica y sus límites independientes.
    return sorted(set(invalid))


def main() -> int:
    result = {
        "check": "dynamic_m1_deployment",
        "scope": "local_settings_only",
        "status": "FAIL",
    }
    try:
        if os.path.lexists(".env"):
            result.update(reason="LOCAL_DOTENV_NOT_ALLOWED", fields=[])
            print(json.dumps(result, sort_keys=True))
            return 2
        settings = Settings(_env_file=None)
        invalid = blockers(settings)
    except ValidationError as exc:
        # Nunca imprimir ValidationError: incluye entradas potencialmente privadas.
        fields = {
            segment
            for error in exc.errors(include_input=False, include_context=False, include_url=False)
            for segment in error["loc"]
            if isinstance(segment, str) and segment in Settings.model_fields
        }
        result.update(reason="SETTINGS_INVALID", fields=sorted(fields) or ["model"])
    except Exception:
        result.update(reason="LOCAL_SETTINGS_UNAVAILABLE", fields=[])
    else:
        if invalid:
            result.update(reason="DEPLOYMENT_PROFILE_MISMATCH", fields=invalid)
        else:
            result.update(status="PASS", mode="armed" if settings.TRADING_ENABLED else "unarmed")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
