"""
Configuración centralizada del bot.

Toda la configuración viene de variables de entorno (que Coolify inyecta).
Pydantic valida tipos y valores al arranque - si algo está mal, el bot
no inicia (fail fast).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración del bot validada al arranque."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # === Kalshi ===
    KALSHI_ENV: Literal["demo", "production"] = "demo"
    KALSHI_API_KEY_ID: str = Field(..., min_length=10)
    KALSHI_PRIVATE_KEY_PATH: Path = Path("/app/secrets/kalshi_private_key.pem")

    # URLs (no overrides en .env normalmente, derivadas de KALSHI_ENV)
    KALSHI_DEMO_REST_URL: str = "https://demo-api.kalshi.co/trade-api/v2"
    KALSHI_DEMO_WS_URL: str = "wss://demo-api.kalshi.co/trade-api/ws/v2"
    KALSHI_PROD_REST_URL: str = "https://api.elections.kalshi.com/trade-api/v2"
    KALSHI_PROD_WS_URL: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    # === Database ===
    DATABASE_URL: str = "sqlite:////app/data/trades.db"

    # === Risk Management (hardcoded defaults) ===
    MAX_DAILY_LOSS_PCT: float = Field(3.0, gt=0, le=10)
    MAX_WEEKLY_LOSS_PCT: float = Field(8.0, gt=0, le=20)
    MAX_MONTHLY_LOSS_PCT: float = Field(15.0, gt=0, le=30)
    MAX_SIMULTANEOUS_EXPOSURE_PCT: float = Field(25.0, gt=0, le=100)
    MAX_TRADE_SIZE_PCT: float = Field(5.0, gt=0, le=20)
    KELLY_FRACTION: float = Field(0.25, gt=0, le=1)
    MIN_EDGE_PCT: float = Field(2.0, gt=0, le=20)
    MIN_LIQUIDITY_CONTRACTS: int = Field(10, ge=1)

    # === Capital ===
    ACTIVE_CAPITAL_USD: float = Field(300.0, gt=0, le=100_000)

    # === Telegram ===
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # === Health server ===
    HEALTH_HOST: str = "0.0.0.0"
    HEALTH_PORT: int = Field(8080, gt=0, le=65535)

    # === Logging ===
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # === Operational toggles ===
    TRADING_ENABLED: bool = False
    MOTOR_1_ARBITRAGE_ENABLED: bool = False
    MOTOR_2_SPORTSBOOK_ENABLED: bool = False
    MOTOR_3_CLV_ENABLED: bool = False
    USE_ORDERBOOK_MANAGER_V2: bool = Field(default=False, description="Enable WS-based recovery (OrderbookManagerV2)")

    # === Motor REST (arbitraje WS-detección + REST-ejecución) ===
    # MOTOR_REST_ENABLED controla si el motor CORRE (se conecta, parsea, detecta,
    # graba EdgeWindow). Default False. Para shadow mode = True + TRADING_ENABLED=False.
    MOTOR_REST_ENABLED: bool = Field(default=False, description="Run Motor REST (shadow/live)")
    # Umbrales calibrables con data real del shadow (NO hardcodear en el motor).
    MOTOR_REST_MIN_EDGE_CENTS: int = Field(
        default=1, ge=0, description="Edge neto post-comisión mínimo para disparar (cents)"
    )
    MOTOR_REST_MIN_DEPTH: int = Field(
        default=2, ge=1, description="Profundidad mínima (contratos) en la pata limitante"
    )

    # === Optional ===
    SENTRY_DSN: str = ""

    # ---- Validators ----

    @field_validator("KALSHI_PRIVATE_KEY_PATH")
    @classmethod
    def _key_must_exist(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(
                f"Private key no encontrada en {v}. "
                f"Verifica que esté montada como volume en Coolify "
                f"o ejecuta scripts/generate_keys.sh localmente."
            )
        if v.stat().st_mode & 0o077:
            # Aviso suave - en Coolify puede estar como root
            pass
        return v

    @model_validator(mode="after")
    def _production_safety(self) -> Settings:
        """
        Validaciones extra cuando estamos en producción real.
        Más estricto que demo.
        """
        if self.KALSHI_ENV == "production":
            if self.ACTIVE_CAPITAL_USD > 5000:
                raise ValueError(
                    f"ACTIVE_CAPITAL_USD={self.ACTIVE_CAPITAL_USD} excede límite de seguridad ($5k). "
                    "Si quieres operar con más capital, modifica este check explícitamente."
                )
            if self.TRADING_ENABLED and not any([
                self.MOTOR_1_ARBITRAGE_ENABLED,
                self.MOTOR_2_SPORTSBOOK_ENABLED,
                self.MOTOR_3_CLV_ENABLED,
                self.MOTOR_REST_ENABLED,
            ]):
                raise ValueError(
                    "TRADING_ENABLED=true pero ningún motor está habilitado. "
                    "Activa al menos un MOTOR_X_*_ENABLED."
                )
        return self

    # ---- Properties ----

    @property
    def rest_url(self) -> str:
        return self.KALSHI_DEMO_REST_URL if self.KALSHI_ENV == "demo" else self.KALSHI_PROD_REST_URL

    @property
    def ws_url(self) -> str:
        return self.KALSHI_DEMO_WS_URL if self.KALSHI_ENV == "demo" else self.KALSHI_PROD_WS_URL

    @property
    def is_production(self) -> bool:
        return self.KALSHI_ENV == "production"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN and self.TELEGRAM_CHAT_ID)

    @property
    def any_motor_enabled(self) -> bool:
        return any([
            self.MOTOR_1_ARBITRAGE_ENABLED,
            self.MOTOR_2_SPORTSBOOK_ENABLED,
            self.MOTOR_3_CLV_ENABLED,
        ])


# Lazy singleton
_settings: Settings | None = None


def get_settings() -> Settings:
    """Obtener singleton de Settings (instancia única en runtime)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_testing() -> None:
    """Solo para tests - permite re-leer .env modificado."""
    global _settings
    _settings = None
