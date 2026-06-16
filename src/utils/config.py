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
    USE_ORDERBOOK_MANAGER_V2: bool = Field(
        default=False, description="Enable WS-based recovery (OrderbookManagerV2)"
    )

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
    # Umbral FINO de EJECUCIÓN, distinto del trigger grueso de arriba: se DETECTA/graba
    # con el grueso (MOTOR_REST_MIN_EDGE_CENTS) pero se EJECUTA solo si el edge neto
    # post-comisión supera este % del capital comprometido (= opp.edge_pct, una fuente
    # de verdad). Default ALTO/conservador; se calibra con data del shadow.
    MOTOR_REST_EXECUTION_EDGE_PCT: float = Field(
        default=1.5,
        ge=0.0,
        description="Edge neto post-fee mínimo (% del capital comprometido) para EJECUTAR",
    )
    # TECHO anti-fantasma: un arb 1X2 real (3 outcomes ~100¢) rara vez supera pocos %.
    # Un edge enorme (ej. 132% visto en shadow 2026-06-16) implica patas que NO suman ~100:
    # pata ~0¢ de equipo eliminado, quote stale-pero-fresca, o grupo/mercado a medio resolver.
    # NO es un regalo — es casi siempre una señal fantasma no-fillable. Se DETECTA/graba igual
    # (EdgeWindow para análisis), pero NO se EJECUTA por encima de este %. Default conservador.
    MOTOR_REST_MAX_EDGE_PCT: float = Field(
        default=10.0, gt=0.0, description="Techo de edge para EJECUTAR (anti-fantasma, %)"
    )

    # === Optional ===
    SENTRY_DSN: str = ""

    # === Motor 2 (consenso sportsbooks) ===
    # API key de The Odds API. Vacía por default; el cliente la lee de acá.
    # ENCENDIDO POR CONFIG: con la key seteada, el runner usa LiveOddsSource (odds reales);
    # vacía → FakeOddsSource (fixture, shadow). No requiere editar código para el flip.
    ODDS_API_KEY: str = ""
    # Deportes a consultar en The Odds API (sport_keys separados por coma). MLB diario =
    # "baseball_mlb" (el play sostenible cuando acabe el Mundial). El Mundial es
    # "soccer_fifa_world_cup". Para correr ambos en transición: "baseball_mlb,soccer_fifa_world_cup"
    # (ojo: cada sport_key extra consume más créditos). Confirmá el key con get_sports() la 1ra vez.
    ODDS_API_SPORT_KEYS: str = "baseball_mlb"
    # Regiones de casas. "eu" incluye Pinnacle (la más afilada) → fair-value más preciso;
    # se suma "us" por cobertura. 1 crédito por región por call (h2h).
    ODDS_API_REGIONS: str = "eu,us"
    # Umbral de edge NETO post-comisión de Motor 2, en PUNTOS PORCENTUALES (3.0 = 3pp).
    # Tuneable sin tocar código: subilo (ej. 4-5) para filtrar señales marginales pegadas
    # al borde. NO confundir con MIN_EDGE_PCT (ese es de Motor 1; Motor 2 usa SOLO este).
    MOTOR_2_MIN_EDGE_PCT: float = Field(
        default=3.0, ge=0.0, description="Edge neto post-fee mínimo de Motor 2 (pp)"
    )

    # === Analyst Loop (loop engineering — ADVISORY ONLY, no tradea) ===
    # Bucle agendado que observa EdgeWindow + el embudo de Motor 2, computa un veredicto
    # (eficiente / matching_bug / edge_candidato), lo persiste (memoria día-a-día) y manda
    # un digest a Telegram. NUNCA tradea, ni cambia config, ni mergea. Default off.
    ANALYST_LOOP_ENABLED: bool = False
    ANALYST_LOOP_INTERVAL_SEC: float = Field(
        default=86400.0, ge=3600.0, description="Intervalo del Analyst Loop (s, mín 1h)"
    )
    ANALYST_LOOP_WINDOW_HOURS: int = Field(
        default=24, ge=1, description="Ventana de agregación del veredicto (horas)"
    )

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
            if self.TRADING_ENABLED and not any(
                [
                    self.MOTOR_1_ARBITRAGE_ENABLED,
                    self.MOTOR_2_SPORTSBOOK_ENABLED,
                    self.MOTOR_3_CLV_ENABLED,
                    self.MOTOR_REST_ENABLED,
                ]
            ):
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
        return any(
            [
                self.MOTOR_1_ARBITRAGE_ENABLED,
                self.MOTOR_2_SPORTSBOOK_ENABLED,
                self.MOTOR_3_CLV_ENABLED,
            ]
        )


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
