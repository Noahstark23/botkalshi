"""Preflight local de configuración para la unidad Bank50.

Rechaza cualquier .env en cwd sin leerlo: el runner sí podría cargarlo. Lee el
entorno efectivo con Settings(_env_file=None), sin singleton ni .env. El
validador Settings comprueba existencia de la ruta PEM y puede parsear el PEM
entregado por entorno. No consulta DB, cuentas, tarifas ni APIs; tampoco arma
flags. Un PASS valida esta configuración, no autoriza órdenes ni certifica saldo.
Uso en el WorkingDirectory del release: python -m scripts.check_bank50_deployment
"""

from __future__ import annotations

import json
import os

from pydantic import ValidationError

from src.utils.config import Settings

DATABASE_URL = "sqlite:////var/lib/botkalshi-runtime/trades.db"
REST_URL = "https://api.elections.kalshi.com/trade-api/v2"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# Se permite estudiar M1 sin ejecución. Los otros ciclos/gestores quedarían fuera
# del único perfil financiero revisado y algunos gastarían cuota o enviarían mensajes.
DISABLED_FLAGS = (
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
)
REQUIRED_FLAGS = ("DB_MAINTENANCE_ENABLED", "DISK_GUARD_ENABLED")
EMPTY_FIELDS = ("ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def blockers(settings: Settings) -> list[str]:
    """Devuelve sólo nombres constantes de campos, jamás sus valores efectivos."""
    invalid = []
    exact = {
        "EXPERIMENT_BANK50_ENABLED": True,
        "EXPERIMENT_BANK200_ENABLED": False,
        "KALSHI_ENV": "production",
        "DATABASE_URL": DATABASE_URL,
        "KALSHI_PROD_REST_URL": REST_URL,
        "KALSHI_PROD_WS_URL": WS_URL,
        "HEALTH_HOST": "127.0.0.1",
        "HEALTH_PORT": 18080,
    }
    for name, expected in exact.items():
        if getattr(settings, name) != expected:
            invalid.append(name)
    # También verificar propiedades derivadas: la rama de entorno debe apuntar
    # exactamente a esos endpoints, no a demo ni a un override de otro host/path.
    if settings.rest_url != REST_URL and "KALSHI_PROD_REST_URL" not in invalid:
        invalid.append("KALSHI_PROD_REST_URL")
    if settings.ws_url != WS_URL and "KALSHI_PROD_WS_URL" not in invalid:
        invalid.append("KALSHI_PROD_WS_URL")
    invalid.extend(name for name in DISABLED_FLAGS if getattr(settings, name))
    invalid.extend(name for name in REQUIRED_FLAGS if not getattr(settings, name))
    # Ni un token suelto de Telegram queda permitido aunque telegram_configured=False.
    invalid.extend(name for name in EMPTY_FIELDS if getattr(settings, name) != "")
    if not settings.TRADING_ENABLED:
        if settings.EXPERIMENT_BANK_CONFIRMED_USD != 0:
            invalid.append("EXPERIMENT_BANK_CONFIRMED_USD")
        if settings.MOTOR_1_EXECUTION_ENABLED:
            invalid.append("MOTOR_1_EXECUTION_ENABLED")
    else:
        # Settings ya exige estos gates Bank50; verificarlos aquí deja explícito el
        # contrato de despliegue sin activar ni modificar ningún flag.
        if settings.EXPERIMENT_BANK_CONFIRMED_USD != 50:
            invalid.append("EXPERIMENT_BANK_CONFIRMED_USD")
        if not settings.MOTOR_1_ARBITRAGE_ENABLED:
            invalid.append("MOTOR_1_ARBITRAGE_ENABLED")
        if not settings.MOTOR_1_EXECUTION_ENABLED:
            invalid.append("MOTOR_1_EXECUTION_ENABLED")
    return sorted(set(invalid))


def main() -> int:
    result = {"check": "bank50_deployment", "scope": "local_settings_only", "status": "FAIL"}
    try:
        # lexists incluye archivos ilegibles, directorios y enlaces rotos. Usar cwd
        # igual que el runner; no la carpeta donde se encuentra este script.
        if os.path.lexists(".env"):
            result.update(reason="LOCAL_DOTENV_NOT_ALLOWED", fields=[])
            print(json.dumps(result, sort_keys=True))
            return 2
        settings = Settings(_env_file=None)
        invalid = blockers(settings)
    except ValidationError as exc:
        # No usar str/repr(exc): Pydantic incluye input_value y puede mostrar PEM,
        # tokens o el dict de entorno completo en errores de un model_validator.
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
