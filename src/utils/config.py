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
    # Cap ABSOLUTO por orden (anti-slippage), en USD — independiente del % y del capital.
    # El size final = min(liquidez_book, kelly/%, MAX_TRADE_SIZE_USD). A $4k el 5% ya da
    # $200; este techo lo BLINDA aunque el capital o el % suban.
    MAX_TRADE_SIZE_USD: float = Field(200.0, gt=0, le=10_000)
    KELLY_FRACTION: float = Field(0.25, gt=0, le=1)
    MIN_EDGE_PCT: float = Field(2.0, gt=0, le=20)
    MIN_LIQUIDITY_CONTRACTS: int = Field(10, ge=1)

    # === Capital ===
    # Bankroll base. Stop-losses (-3/-8/-15%) se derivan de acá → -$120/-$320/-$600 a $4k.
    ACTIVE_CAPITAL_USD: float = Field(4000.0, gt=0, le=100_000)
    # C-01: cada cuántos segundos el RiskManager refresca el balance REAL de Kalshi (cash) que
    # usa como capital base. ACTIVE_CAPITAL_USD pasa a ser solo el PISO de seguridad (fallback
    # si la API nunca respondió). Mín 30s para no martillar la API.
    BALANCE_REFRESH_SECONDS: int = Field(
        default=300, ge=30, description="Refresh del balance real (s)"
    )
    # C-02: factor de seguridad sobre el cash real. El capital base de riesgo pasa a ser
    # min(cash_real × este %, hard cap de prod $5k). Colchón anti-desfase: nunca arriesgar
    # el 100% del cash (slippage, fills parciales, el balance se mueve entre refresh). SOLO
    # aplica al cash REAL; el fallback ACTIVE_CAPITAL_USD ya es un piso conservador, no se
    # factoriza. 100% = usar el cash completo (sin colchón).
    CAPITAL_SAFETY_FACTOR_PCT: float = Field(
        default=90.0, gt=0, le=100, description="% del cash real usable como capital base"
    )
    # C-03: si |cash real − ACTIVE_CAPITAL_USD| supera este % del config, alerta por Telegram
    # (desfase entre el param de Coolify y el cash real). Advisory: NO cambia el sizing, solo
    # avisa para que actualices el config o investigues el movimiento de cash.
    CAPITAL_DRIFT_ALERT_PCT: float = Field(
        default=25.0, gt=0, description="Umbral de desfase config↔cash real para alertar (%)"
    )
    # === Capital dinámico (extensión sobre C-01/02/03) ===
    # Master toggle: False → el RiskManager IGNORA el cash real y usa ACTIVE_CAPITAL_USD fijo
    # (escudo / dry-run en staging). La reserva sigue siendo CAPITAL_SAFETY_FACTOR_PCT y el
    # intervalo de refresh BALANCE_REFRESH_SECONDS — no se duplican.
    DYNAMIC_CAPITAL_ENABLED: bool = Field(
        default=True,
        description="Usar el balance real de Kalshi como capital base (False = ACTIVE_CAPITAL_USD fijo)",
    )
    # Piso absoluto en USD: el capital efectivo nunca baja de acá (la matemática de riesgo no
    # se rompe), pero por debajo del piso se PAUSAN las NUEVAS entradas (can_open_new_positions
    # = False) mientras la gestión/cierre de lo abierto sigue operando.
    CAPITAL_FLOOR_USD: float = Field(
        default=100.0,
        gt=0,
        description="Piso de capital efectivo (USD); debajo pausa nuevas entradas",
    )
    # Techo configurable en USD sobre el capital derivado del cash real (además del hard cap de
    # prod $5k, que sigue como backstop). Acota el riesgo aunque el cash crezca.
    CAPITAL_CAP_USD: float = Field(
        default=2000.0, gt=0, description="Techo de capital efectivo (USD) sobre el cash real"
    )
    # Anti-churn: si el capital objetivo cambia menos que esta fracción vs el último, se ignora
    # (no actualiza la caché de riesgo). 0.05 = 5%. 0 = sin suavizado (siempre actualiza).
    CAPITAL_SMOOTHING_PCT: float = Field(
        default=0.05,
        ge=0,
        le=1,
        description="Fracción mínima de cambio para refrescar el capital (anti-churn)",
    )
    # Bankroll inicial REAL en USD para la reconciliación de balance del dashboard
    # (scripts/check_portfolio.py). Solo lectura/observabilidad — NO afecta sizing ni riesgo.
    # 0.0 = sin setear → el dashboard usa DailyPnL.starting_capital si existe, o lo omite.
    KALSHI_INITIAL_BANKROLL: float = Field(0.0, ge=0)

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
    # Umbral FINO de EJECUCIÓN del Motor 1 (binario WS), distinto del MIN_EDGE_PCT de
    # DETECCIÓN: se DETECTA/graba EdgeWindow con MIN_EDGE_PCT pero se EJECUTA solo si el
    # edge neto post-fee supera este % (= opp.edge_pct, una fuente de verdad). Conservador.
    MOTOR_1_EXECUTION_EDGE_PCT: float = Field(
        default=1.5,
        ge=0.0,
        description="Edge neto post-fee mínimo (% del capital comprometido) para EJECUTAR (Motor 1)",
    )
    # TECHO anti-fantasma del Motor 1: un edge enorme NO es un regalo — es casi siempre una
    # pata sin precio / mercado a medio resolver. Se DETECTA/graba igual (EdgeWindow) pero
    # NO se EJECUTA por encima de este %. Mismo default conservador que el Motor REST.
    MOTOR_1_MAX_EDGE_PCT: float = Field(
        default=10.0, gt=0.0, description="Techo de edge para EJECUTAR (anti-fantasma, %) (Motor 1)"
    )
    MOTOR_2_SPORTSBOOK_ENABLED: bool = False
    MOTOR_3_CLV_ENABLED: bool = False
    MOTOR_3_EXECUTION_ENABLED: bool = Field(
        default=False,
        description="Si es True (y TRADING_ENABLED es True), Motor 3 VENDERÁ posiciones. Si es False, solo detecta y loguea (shadow).",
    )
    # FASE 1 — Take-profit por precio (salida por bid≥umbral, junto a la salida por tiempo
    # T-30min). TAKE_PROFIT_ENABLED gatea la DETECCIÓN (+log shadow); la EJECUCIÓN la sigue
    # gateando MOTOR_3_EXECUTION_ENABLED (misma Capa A). Shadow = ENABLED True + EXECUTION False.
    MOTOR_3_TAKE_PROFIT_ENABLED: bool = Field(
        default=False,
        description="Si es True, Motor 3 detecta+loguea take-profit (bid≥umbral). La venta real sigue gateada por MOTOR_3_EXECUTION_ENABLED.",
    )
    MOTOR_3_TAKE_PROFIT_CENTS: int = Field(
        default=90,
        ge=1,
        le=99,
        description="Umbral del bid (cents) del lado abierto para el take-profit de Motor 3",
    )
    # FASE 2 — Trailing stop (salida por retroceso del bid desde su pico, solo en ganancia).
    # ENABLED gatea la detección (+log shadow); la venta la sigue gateando MOTOR_3_EXECUTION_ENABLED.
    MOTOR_3_TRAILING_ENABLED: bool = Field(
        default=False,
        description="Si es True, Motor 3 detecta+loguea trailing stop (retroceso del bid). La venta real sigue gateada por MOTOR_3_EXECUTION_ENABLED.",
    )
    MOTOR_3_TRAILING_DROP_CENTS: int = Field(
        default=5,
        ge=1,
        le=99,
        description="Retroceso (cents) desde el pico que dispara el trailing stop",
    )
    # Motor 2 — cierre de posiciones (take-profit / trailing), espejo del de Motor 3. Motor 2
    # SOLO abría posiciones (ride-to-settlement) → PnL histórico negativo por asimetría
    # (avg_loss ≫ avg_win). Mismo esquema de dos capas: ENABLED gatea la DETECCIÓN (+log
    # [MOTOR 2 TP SHADOW] con net de fees); la VENTA real la gatea MOTOR_2_EXECUTION_ENABLED.
    # Shadow = TAKE_PROFIT_ENABLED True + EXECUTION_ENABLED False.
    MOTOR_2_EXECUTION_ENABLED: bool = Field(
        default=False,
        description="Si es True (y TRADING_ENABLED es True), Motor 2 VENDERÁ posiciones al disparar take-profit/trailing. Si es False, solo detecta y loguea (shadow).",
    )
    MOTOR_2_TAKE_PROFIT_ENABLED: bool = Field(
        default=False,
        description="Si es True, Motor 2 detecta+loguea take-profit (bid≥umbral) sobre sus posiciones abiertas. La venta real sigue gateada por MOTOR_2_EXECUTION_ENABLED.",
    )
    MOTOR_2_TAKE_PROFIT_CENTS: int = Field(
        default=90,
        ge=1,
        le=99,
        description="Umbral del bid (cents) del lado abierto para el take-profit de Motor 2 (calibrable; backtest MLB ≈62)",
    )
    MOTOR_2_TRAILING_ENABLED: bool = Field(
        default=False,
        description="Si es True, Motor 2 detecta+loguea trailing stop (retroceso del bid). La venta real sigue gateada por MOTOR_2_EXECUTION_ENABLED.",
    )
    MOTOR_2_TRAILING_DROP_CENTS: int = Field(
        default=5,
        ge=1,
        le=99,
        description="Retroceso (cents) desde el pico que dispara el trailing stop de Motor 2",
    )
    USE_ORDERBOOK_MANAGER_V2: bool = Field(
        default=False, description="Enable WS-based recovery (OrderbookManagerV2)"
    )

    # === Motor REST (arbitraje WS-detección + REST-ejecución) ===
    # MOTOR_REST_ENABLED controla si el motor CORRE (se conecta, parsea, detecta,
    # graba EdgeWindow). Default False. Para shadow mode = True + TRADING_ENABLED=False.
    MOTOR_REST_ENABLED: bool = Field(default=False, description="Run Motor REST (shadow/live)")
    # Desacopla la EJECUCIÓN del Motor REST del TRADING_ENABLED global (igual que
    # MOTOR_3_EXECUTION_ENABLED): con MOTOR_REST_ENABLED=True + esto en False, el motor corre
    # en SHADOW (detecta + graba EdgeWindow) aunque el trading global esté ON → valida el
    # guardarraíl pata-dura-primero (#85) sin ejecutar ni apagar a los otros motores.
    MOTOR_REST_EXECUTION_ENABLED: bool = Field(
        default=False,
        description="Si es True (y TRADING_ENABLED es True), Motor REST EJECUTA órdenes. Si es False, solo detecta y loguea (shadow).",
    )
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
    # Series de Kalshi que MOTOR 2 consume (su universo de quotes). DESACOPLADO de
    # MULTI_SERIES (Motor REST, winner-take-all ≥3 legs): Motor 2 también opera 2-way
    # (MLB/NBA moneyline) que el arb NO toca. Coma-separado, tuneable por env — sumar
    # KXNBAGAME/KXNHLGAME aquí onboarda esos deportes a Motor 2 sin tocar código.
    MOTOR2_SERIES: str = "KXMLBGAME,KXWCGAME,KXWCGROUPWIN,KXMENWORLDCUP,KXMWORLDCUP"
    # Umbral de edge NETO post-comisión de Motor 2, en PUNTOS PORCENTUALES (3.0 = 3pp).
    # Tuneable sin tocar código: subilo (ej. 4-5) para filtrar señales marginales pegadas
    # al borde. NO confundir con MIN_EDGE_PCT (ese es de Motor 1; Motor 2 usa SOLO este).
    MOTOR_2_MIN_EDGE_PCT: float = Field(
        default=3.0, ge=0.0, description="Edge neto post-fee mínimo de Motor 2 (pp)"
    )
    # Filtro underdog (FASE 3): las entradas <40c sangraron −$110,77 en el histórico. ENABLED
    # off = SHADOW intra-live (loguea lo que bloquearía pero igual entra); on = bloquea.
    MOTOR_2_MIN_ENTRY_CENTS: int = Field(
        default=40,
        ge=1,
        le=99,
        description="Precio mínimo (cents) para ejecutar Motor 2 (underdog filter)",
    )
    MOTOR_2_UNDERDOG_FILTER_ENABLED: bool = Field(
        default=False,
        description="Si es True, Motor 2 BLOQUEA entradas <MOTOR_2_MIN_ENTRY_CENTS. Si es False, solo loguea (shadow).",
    )
    # Mutua exclusión por EVENTO: con True (default) Motor 2 emite UNA sola apuesta direccional
    # por partido (la de mayor edge neto), aunque el edge aparezca en varios outcomes/markets del
    # mismo evento. Previene la doble exposición correlacionada que sangró −$218 (yes en el market
    # de un equipo + no en el del otro = misma dirección, en market_tickers distintos → un dedup
    # por ticker no lo agarra). Acota además la exposición por partido a un solo trade. Escape
    # hatch: False restaura el comportamiento previo (una señal por cada outcome/lado con edge).
    MOTOR_2_ONE_BET_PER_EVENT: bool = Field(
        default=True,
        description="Si es True, Motor 2 emite una sola apuesta (mayor edge) por evento/partido.",
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

    # Resumen diario de P&L por Telegram (advisory-only). Una vez/día computa el P&L
    # REALIZADO del día UTC anterior por motor, lo persiste en DailyPnL (memoria + /health
    # + check_portfolio) y manda el digest. Default off. NO tradea ni toca capital.
    DAILY_PNL_REPORT_ENABLED: bool = False
    DAILY_PNL_REPORT_INTERVAL_SEC: float = Field(
        default=3600.0, ge=300.0, description="Intervalo del check del reporte diario (s)"
    )

    # Monitor de memoria (advisory-only): lee el uso real del cgroup y avisa por Telegram
    # cuando cruza el umbral del límite del contenedor. Aviso temprano de crecimiento
    # orgánico del baseline (más mercados → más working-set) ANTES del OOM kill, en vez de
    # enterarse por los reinicios. NUNCA tradea ni toca capital.
    MEMORY_MONITOR_ENABLED: bool = True
    MEMORY_MONITOR_THRESHOLD_PCT: float = Field(
        default=80.0, gt=0, le=100, description="Umbral de uso de memoria para alertar (%)"
    )
    MEMORY_MONITOR_INTERVAL_SEC: int = Field(
        default=60, ge=10, description="Intervalo del monitor de memoria (s)"
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
                    self.MOTOR_3_EXECUTION_ENABLED,
                    self.MOTOR_REST_ENABLED,
                    self.MOTOR_REST_EXECUTION_ENABLED,
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
