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
    # Pisos en USD de los stop-losses (problema de escala, 2026-07-12): con capital chico
    # los % producen límites a nivel de RUIDO (3% de $180 = $5.40 → 2-3 trades chicos
    # perdedores apagaban TODO el bot y exigían clear manual). El límite efectivo es
    # max(capital × %, piso): con capital grande manda el % (igual que siempre); con
    # capital chico el piso evita el falso positivo. Piso 0 = comportamiento histórico.
    MAX_DAILY_LOSS_FLOOR_USD: float = Field(
        default=20.0, ge=0, description="Piso en USD del stop-loss diario (0 = solo %)"
    )
    MAX_WEEKLY_LOSS_FLOOR_USD: float = Field(
        default=40.0, ge=0, description="Piso en USD del stop-loss semanal (0 = solo %)"
    )
    MAX_MONTHLY_LOSS_FLOOR_USD: float = Field(
        default=60.0, ge=0, description="Piso en USD del stop-loss mensual (0 = solo %)"
    )
    # Respuesta ESCALONADA (2026-07-12): el breach DIARIO pausa solo las ENTRADAS nuevas y
    # se auto-recupera en el rollover del día UTC (la ventana se recomputa de DB en cada
    # check) — sin kill-switch persistente ni clear_kill_switch.py. Un día malo es ruido;
    # una racha semanal/mensual es problema estructural: esas ventanas SIGUEN disparando el
    # kill-switch persistente (nuclear, manual). False = comportamiento histórico (diario
    # también nuclear).
    DAILY_STOP_ENTRIES_ONLY: bool = Field(
        default=True,
        description="Breach diario: pausa entradas hasta el día UTC siguiente (auto-recupera) en vez de kill-switch persistente.",
    )
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
    # Dashboard on-demand por Telegram (/dashboard). Long-polling read-only; solo responde al
    # TELEGRAM_CHAT_ID autorizado. No-op si Telegram no está configurado. Default on (advisory).
    TELEGRAM_DASHBOARD_ENABLED: bool = Field(
        default=True, description="Habilita el comando /dashboard de Telegram (read-only)"
    )
    # Envío AUTOMÁTICO del dashboard cada N segundos (0 = solo on-demand). Reusa el mismo builder.
    TELEGRAM_DASHBOARD_INTERVAL_SEC: float = Field(
        default=0.0, ge=0.0, description="Auto-envío del dashboard cada N s (0 = off)"
    )

    # === Health server ===
    HEALTH_HOST: str = "0.0.0.0"
    HEALTH_PORT: int = Field(8080, gt=0, le=65535)

    # === Logging ===
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # === Operational toggles ===
    TRADING_ENABLED: bool = False
    MOTOR_1_ARBITRAGE_ENABLED: bool = False
    # Capa A por motor para las ENTRADAS de Motor 1 (auditoría 2026-07-07, P1): antes el
    # ArbitrageExecutor se construía con TRADING_ENABLED solo — prender el trading global
    # para que M3 venda ARMABA también las compras de M1. Ahora M1 solo compra con los DOS
    # flags (mismo esquema que MOTOR_3/REST/MM). Shadow = ARBITRAGE_ENABLED True + esto False.
    MOTOR_1_EXECUTION_ENABLED: bool = Field(
        default=False,
        description="Si es True (y TRADING_ENABLED es True), Motor 1 COLOCA arbs reales. Si es False, solo detecta y loguea (shadow).",
    )
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
    # Auditoría rentabilidad 2026-07-07: el "arb" binario intra-ticker = book AUTO-CRUZADO
    # (yes_bid+no_bid>100), estado que el matching engine elimina en ms — casi toda señal
    # de 1 tick es book local stale (evidencia: 0 ventanas binarias en TODA la historia;
    # las 3054 EdgeWindow fueron multi-outcome). Confirmación + cooldown acotan el costo
    # de perseguir fantasmas (cada fill asimétrico = rollback = spread + 2 fees).
    MOTOR_1_CONFIRM_TICKS: int = Field(
        default=2,
        ge=1,
        description="Ticks consecutivos (~1s c/u) que el cruce debe persistir antes de EJECUTAR (Motor 1)",
    )
    MOTOR_1_TICKER_COOLDOWN_SEC: float = Field(
        default=60.0,
        ge=0.0,
        description="Cooldown por ticker tras una ejecución fallida de Motor 1 (KILL/rollback) — no re-martillar el mismo cruce stale",
    )
    # Bug 2 (incidente 2026-07-07): cap de exposición DIRECCIONAL por EVENTO (partido). Los
    # tickers hermanos (…HOUWSH-HOU / …HOUWSH-WSH) son el MISMO evento real; los residuales de
    # netting/huérfanas se acumulaban en la misma dirección ($135) sin que nadie los sumara.
    # Si un evento supera este cap, Motor 1 NO coloca más arbs sobre él (guard pre-arb).
    MAX_EVENT_DIRECTIONAL_EXPOSURE_USD: float = Field(
        default=25.0,
        gt=0.0,
        description="Cap de exposición direccional acumulada por evento (USD) para Motor 1",
    )
    MOTOR_2_SPORTSBOOK_ENABLED: bool = False
    # Capa A por motor para las ENTRADAS (apuestas) de Motor 2 (auditoría 2026-07-07, P1):
    # mismo gap que Motor 1 — el Motor2Executor de entrada se construía con TRADING_ENABLED
    # solo. OJO: es DISTINTO de MOTOR_2_EXECUTION_ENABLED (que gatea las VENTAS del brazo de
    # salida, más abajo). Shadow = SPORTSBOOK_ENABLED True + esto False.
    MOTOR_2_ENTRY_EXECUTION_ENABLED: bool = Field(
        default=False,
        description="Si es True (y TRADING_ENABLED es True), Motor 2 APUESTA (abre posiciones). Si es False, solo detecta y loguea (shadow). Las ventas las gatea MOTOR_2_EXECUTION_ENABLED.",
    )
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
    # Bug 4 (incidente 2026-07-07): con True, Motor 3 también gestiona las patas HUÉRFANAS de
    # Motor 1 (BUY fillado cuyo arb quedó sin la pata hermana por rollback abortado). SOLO las
    # huérfanas verdaderas — un par hedged completo JAMÁS se toca (venderlo suelto rompe el
    # hedge, misma razón por la que motor_rest_arb está excluido). Default off (sin cambio).
    MOTOR_3_MANAGES_ORPHANS: bool = Field(
        default=False,
        description="Si es True, Motor 3 aplica take-profit/trailing a huérfanas de Motor 1",
    )
    # Auditoría de rentabilidad 2026-07-07: piso de PRECIO para las VENTAS de salida
    # (T-30/TP/trailing, M3 y el exit de M2). Vender a bid de polvo (1-4c) recupera
    # centavos menos el fee (ceil ≥1c) y dona el spread en el momento de peor liquidez —
    # mejor dejar que la posición settlee (el settle no paga fee). 0 = sin piso.
    MOTOR_3_MIN_SELL_BID_CENTS: int = Field(
        default=5,
        ge=0,
        le=99,
        description="Bid mínimo (cents) para que una salida venda; debajo, la posición va a settle (0 = sin piso)",
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
    # Headroom de la recovery de V2 (tuneable en vivo sin redeploy de código). Con UN solo sid de
    # ~328 tickers, el buffer se llenaba a 5000 antes de que completara la recovery → overflow →
    # circuit breaker → books_initialized=0. Subir si los logs muestran recovered creciendo pero
    # el buffer se llena igual; el timeout acota el tiempo máximo de una recovery atascada.
    ORDERBOOK_V2_MAX_RECOVERY_BUFFER: int = Field(
        default=25000, ge=1000, description="Tope del buffer de recovery de OrderbookManagerV2"
    )
    ORDERBOOK_V2_RECOVERY_TIMEOUT_SEC: float = Field(
        default=30.0, gt=0, description="Timeout (s) de una recovery atascada de OrderbookManagerV2"
    )

    # === Motor 5 (market maker) — F1 SHADOW (docs/motor_5_market_maker_plan_fases.md) ===
    # F1 = cotización HIPOTÉTICA contra el book real, cero órdenes: el executor no existe
    # hasta F2. Shadow = MOTOR_MM_ENABLED=True (EXECUTION queda False y sin efecto).
    MOTOR_MM_ENABLED: bool = Field(
        default=False,
        description="Corre el Motor 5 en F1 shadow (quotes+fills hipotéticos; CERO órdenes)",
    )
    MOTOR_MM_EXECUTION_ENABLED: bool = Field(
        default=False,
        description="RESERVADO F2+: en F1 no existe executor. En producción, True falla el boot (fail-loud, no un no-op engañoso).",
    )
    MOTOR_MM_MAX_TICKERS: int = Field(
        default=10, ge=1, le=100, description="Máx tickers cotizados por tick (Motor 5)"
    )
    MOTOR_MM_HALF_SPREAD_CENTS: int = Field(
        default=3, ge=1, le=20, description="Half-spread alrededor del fair (cents) (Motor 5)"
    )
    MOTOR_MM_QUOTE_SIZE_CONTRACTS: int = Field(
        default=10, ge=1, description="Contratos por lado de cada quote shadow (Motor 5)"
    )
    MOTOR_MM_MAX_INVENTORY_CONTRACTS: int = Field(
        default=50,
        ge=1,
        description="Tope de |inventario| simulado por ticker; al tope se cotiza solo el lado que reduce (Motor 5)",
    )
    # Edge-skew (asymmetric quoting, F2, propuesta 2026-07-13): inclina el centro de las
    # quotes hacia el lado donde el BOOK está desplazado del fair (book bajo el fair → bid
    # más agresivo). Captura más flujo del lado con edge; el post-only y el spread mínimo
    # rentable siguen mandando. 0 = off (F1 histórico exacto).
    MOTOR_MM_EDGE_SKEW_CENTS: int = Field(
        default=0,
        ge=0,
        le=5,
        description="Lean del centro de quotes hacia el lado con edge vs el book (¢). 0 = off.",
    )
    MOTOR_MM_FAIR_TTL_SEC: float = Field(
        default=600.0,
        gt=0,
        description="Edad máx del fair de Motor 2 para cotizar (2 ciclos del poller por default)",
    )
    # LLAVE F3 (plan §5.3): la ejecución del MM en PRODUCCIÓN exige el OK explícito de
    # Noel, documentado. OK otorgado 2026-07-02 ("tienes mi ok") — pero el ORDEN de §5
    # manda: smoke test (scripts/motor5_smoke_test.py) ANTES de girar esta llave. El
    # valor exacto obliga a un acto deliberado en el env de Coolify, no un typo.
    MOTOR_MM_F3_ACK: str = Field(
        default="",
        description="Llave de activación F3 en producción. Valor requerido: 'NOEL-OK-F3'. Vacío = producción bloqueada (demo no la necesita).",
    )
    # Canary cap del MM en producción (plan §5: 'capital canary topado — $100, techo duro
    # por config'): tope ABSOLUTO del costo abierto (pending+filled) del Motor 5, aparte
    # del headroom global del RiskManager. Primera semana: inventario a la mitad de demo.
    MOTOR_MM_MAX_EXPOSURE_USD: float = Field(
        default=100.0,
        gt=0.0,
        description="Techo duro (USD) del costo abierto del Motor 5 (canary F3)",
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
    # Universo del path multi-outcome — antes hardcodeado en el engine (auditoría 2026-07-12:
    # solo 4 series del Mundial → fuera de las ventanas de partido, universo vacío = 0
    # evaluaciones). Tuneable en vivo > hardcodeado. ⚠️ SOLO series winner-take-all
    # (mutuamente excluyentes y exhaustivas: exactamente UN outcome gana). NO meter series
    # de props/totals (KXWCTEAMGOALS, KXWCGAMEGOALS…): sus markets NO son excluyentes →
    # comprar YES en todos NO es arb → señal falsa que EJECUTA plata real.
    MOTOR_REST_MULTI_SERIES: str = Field(
        default="KXWCGAME,KXWCGROUPWIN,KXMENWORLDCUP,KXMWORLDCUP",
        description="Series winner-take-all del path multi-outcome (separadas por coma, match por serie EXACTA).",
    )
    MOTOR_REST_MULTI_MAX_QUOTE_AGE_SEC: float = Field(
        default=30.0,
        gt=0,
        description="Edad máxima de quote por pata: una pata stale → grupo NO se evalúa (anti-fantasma).",
    )
    MOTOR_REST_MULTI_MIN_LEGS: int = Field(
        default=3,
        ge=3,
        description="Mínimo de outcomes para evaluar un evento multi (WTA real exige ≥3; no bajar).",
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
    # Techo de edge de EJECUCIÓN de Motor 2 (anti-fantasma), en PUNTOS PORCENTUALES.
    # Espejo de MOTOR_1_MAX_EDGE_PCT. Auditoría de trades reales: el bucket de edge
    # ~5% fue rentable (+$189, 59% win) pero 12-13% sangró (−$621, ≤54% win). Un
    # "edge consensus" alto en MLB es casi siempre artefacto (consenso mal calibrado).
    # Se DETECTA/loguea pero NO se emite señal ejecutable por encima de este umbral.
    # NO confundir con el backstop MAX_PLAUSIBLE_EDGE (15%, artefactos monstruosos).
    MOTOR_2_MAX_EDGE_PCT: float = Field(
        default=8.0,
        gt=0.0,
        description="Techo de edge para EJECUTAR Motor 2 (anti-fantasma, pp)",
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
        # Default True desde la auditoría de rentabilidad 2026-07-07: el shadow intra-live
        # ya cumplió su función — el histórico es concluyente (<40c: −$110,77, 17/21
        # perdedoras; consistente con el sesgo favorito-longshot del de-vig multiplicativo,
        # que sobreestima el fair del underdog). Dirección del cambio: BLOQUEA (conservador).
        default=True,
        description="Si es True, Motor 2 BLOQUEA entradas <MOTOR_2_MIN_ENTRY_CENTS. Si es False, solo loguea (shadow).",
    )
    # Auditoría rentabilidad 2026-07-07 — endurecimiento de la MEDICIÓN del consenso:
    # el único gate previo era >=2 OUTCOMES; UNA sola casa soft podía formar el "consenso"
    # entero (n_books solo se logueaba), y una línea congelada hace horas pesaba igual
    # que una fresca. Con pocas casas el edge medido puede ser 100% ruido.
    MOTOR_2_MIN_BOOKS: int = Field(
        default=3,
        ge=1,
        description="Mínimo de casas con set completo para que exista consenso (fair) en Motor 2",
    )
    MOTOR_2_MAX_BOOK_AGE_MIN: float = Field(
        default=15.0,
        ge=0.0,
        description="Edad máxima (min) del last_update de una casa para entrar al consenso; 0 = sin filtro",
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
    # Sizing FLAT: con > 0, el stake de cada señal de Motor 2 = capital × pct/100 (fracción fija),
    # DESACOPLADO del edge. Kelly escala el stake con (true_prob − ask) y sobre-apuesta donde el
    # edge está sobreestimado (consenso ruidoso, MLB con pocas casas) → asimetría que sangró −19%
    # ROI (sim sobre 141 settled: flat constante +22.9%). Default 1.0% conservador. 0 = ¼ Kelly
    # (comportamiento previo). El RiskManager re-capea aguas abajo (capital efectivo + cap absoluto).
    MOTOR_2_MAX_STAKE_PCT: float = Field(
        default=1.0,
        ge=0.0,
        le=5.0,
        description="Stake flat por trade de Motor 2 (% del capital). 0 = ¼ Kelly (previo).",
    )
    # Precisión de la fee en el edge (auditoría 2026-07-12): _net_edge_pct medía la fee con
    # count=1; el ceil por ORDEN de Kalshi la sobreestima hasta +0.78pp en asks bajos vs lo
    # que la orden real (stake flat) paga por contrato. On = fee medida al count real.
    # Shadow-first: default OFF. NOTA 2026-07-14: este campo se PERDIÓ en la resolución
    # remota del merge de #155 (quedó el consumo en runner/poller sin la declaración) →
    # AttributeError al boot de M2. El test de wiring runner↔Settings ahora lo previene.
    MOTOR_2_FEE_AT_STAKE_COUNT: bool = Field(
        default=False,
        description="Medir la fee del edge al count real del stake flat (más preciso) en vez de count=1 (pesimista).",
    )
    # Burst polling pre-kickoff (auditoría 2026-07-12): los edges reales del funnel son
    # transitorios y se concentran cerca del inicio del partido; con el ritmo base (300s)
    # se ven de casualidad. Con burst > 0, el poller acelera a ese intervalo cuando hay un
    # kickoff dentro de la ventana. Costo: más requests a The Odds API SOLO en esas
    # ventanas (tarifa plana hoy). 0 = off (comportamiento histórico).
    MOTOR_2_BURST_INTERVAL_SEC: float = Field(
        default=0.0,
        ge=0.0,
        description="Intervalo acelerado del poller M2 cuando hay kickoff próximo (s). 0 = off.",
    )
    MOTOR_2_BURST_WINDOW_MIN: float = Field(
        default=45.0,
        gt=0.0,
        description="Ventana pre-kickoff (min) en la que aplica el burst del poller M2.",
    )

    # === Motor 6 (line-move follower) — F1 SHADOW ===
    # Tesis (funnel 2026-07-12): los edges reales son transitorios — nacen cuando las casas
    # MUEVEN la línea y Kalshi tarda en seguirla. M6 detecta el DELTA del fair entre ciclos
    # de M2 (pasajero del mismo loop: cero API extra) y registra la señal. F1 = shadow puro:
    # no existe executor; loguea [MOTOR 6 SHADOW] + graba EdgeWindow kind="linemove".
    MOTOR_6_LINEMOVE_ENABLED: bool = Field(
        default=False,
        description="Corre el shadow del Motor 6 (line-moves) dentro del ciclo de M2. Solo observa.",
    )
    MOTOR_6_MOVE_MIN_PP: float = Field(
        default=3.0,
        gt=0.0,
        description="Movimiento mínimo del fair entre ciclos (pp) para considerar line-move.",
    )
    MOTOR_6_EDGE_MIN_PP: float = Field(
        default=2.0,
        ge=0.0,
        description="Edge neto post-fee mínimo (pp) vs el ask actual (filtra moves ya digeridos).",
    )
    MOTOR_6_MAX_EDGE_PP: float = Field(
        default=10.0,
        gt=0.0,
        description="Techo anti-fantasma (pp): un neto enorme = quote stale, no un regalo.",
    )

    # === Motor 8 (Order Flow Imbalance) — F1 SHADOW auto-validante ===
    # Tesis a validar: un OFI anómalo (z-score de la ventana corta vs su historia) precede
    # al movimiento. Reservas documentadas: books finos + flujo informado (adverse
    # selection). El shadow NO asume dirección: mide el move real a T+30/T+60 y lo graba
    # (EdgeWindow kind="ofi") — F2 decide contrarian vs momentum vs archivar CON datos.
    # Pasajero del feed de deltas: cero API extra, cero persistencia de deltas.
    MOTOR_8_OFI_ENABLED: bool = Field(
        default=False,
        description="Corre el shadow OFI (Motor 8) sobre el feed de deltas. Solo observa y mide.",
    )
    MOTOR_8_OFI_WINDOW_SEC: float = Field(
        default=60.0, gt=0, description="Ventana corta del OFI (s)."
    )
    MOTOR_8_OFI_Z_MIN: float = Field(
        default=3.0, gt=0, description="Z-score mínimo del OFI para registrar señal."
    )
    MOTOR_8_OFI_MIN_BASELINE: int = Field(
        default=200, ge=10, description="Muestras mínimas de historia antes de señalar (madurez)."
    )
    MOTOR_8_OFI_COOLDOWN_SEC: float = Field(
        default=120.0, gt=0, description="Silencio por ticker tras una señal (anti-ráfaga)."
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

    # === Mantenimiento de DB (incidente disco-lleno 2026-07-10) ===
    # orderbook_events se grababa por cada delta del WS (millones/día) y NADIE la lee → llenó
    # el disco. OPT-IN: default off (los books viven en memoria, no hace falta persistirlos).
    PERSIST_ORDERBOOK_EVENTS: bool = Field(
        default=False,
        description="Si es True, persiste cada orderbook_delta a orderbook_events (telemetry pesada; default off).",
    )
    # Loop de retención: poda tablas de DIAGNÓSTICO por ventana + wal_checkpoint. NUNCA toca
    # estado de trading. Default on; acota el crecimiento de la DB para que no se vuelva a llenar.
    DB_MAINTENANCE_ENABLED: bool = Field(
        default=True,
        description="Corre el loop de retención de tablas de diagnóstico + WAL checkpoint.",
    )
    DB_MAINTENANCE_INTERVAL_HOURS: float = Field(
        default=6.0, gt=0, description="Cada cuánto corre el mantenimiento de DB (horas)."
    )
    # DiskGuard: lazo CERRADO de presión de disco. La retención de arriba es lazo abierto —
    # nadie miraba el disco real y el WAL a ~8MB/s llenó el 96% sin que el bot se enterara
    # (incidente 2026-07-10). WARN → alerta Telegram + poda inmediata; CRITICAL → además se
    # descarta telemetría (orderbook_events/market_snapshots). Trading state JAMÁS se gatea.
    DISK_GUARD_ENABLED: bool = Field(
        default=True,
        description="Monitorea disco libre del mount de la DB y hace backpressure de telemetría.",
    )
    DISK_GUARD_INTERVAL_MINUTES: float = Field(
        default=5.0, gt=0, description="Cada cuánto mide el disco el DiskGuard (minutos)."
    )
    DISK_GUARD_WARN_FREE_GB: float = Field(
        default=5.0,
        gt=0,
        description="Umbral WARN: menos de estos GB libres → alerta + poda inmediata.",
    )
    DISK_GUARD_CRITICAL_FREE_GB: float = Field(
        default=2.0,
        gt=0,
        description="Umbral CRITICAL: menos de estos GB libres → además descarta telemetría.",
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
            # Solo los flags que ARRANCAN un motor cuentan (deuda auditoría 2026-07-01:
            # los *_EXECUTION_ENABLED no arrancan nada por sí solos — contaban como
            # "motor habilitado" y el boot pasaba la validación sin ningún motor corriendo).
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
                    "Activa al menos un MOTOR_X_ENABLED (los *_EXECUTION_ENABLED "
                    "no arrancan motores por sí solos)."
                )
            if self.MOTOR_1_EXECUTION_ENABLED and not self.MOTOR_1_ARBITRAGE_ENABLED:
                raise ValueError(
                    "MOTOR_1_EXECUTION_ENABLED=true requiere MOTOR_1_ARBITRAGE_ENABLED=true "
                    "(la ejecución sin el motor corriendo es un no-op engañoso)."
                )
            if self.MOTOR_2_ENTRY_EXECUTION_ENABLED and not self.MOTOR_2_SPORTSBOOK_ENABLED:
                raise ValueError(
                    "MOTOR_2_ENTRY_EXECUTION_ENABLED=true requiere "
                    "MOTOR_2_SPORTSBOOK_ENABLED=true "
                    "(la ejecución sin el motor corriendo es un no-op engañoso)."
                )
            if self.MOTOR_3_EXECUTION_ENABLED and not self.MOTOR_3_CLV_ENABLED:
                raise ValueError(
                    "MOTOR_3_EXECUTION_ENABLED=true requiere MOTOR_3_CLV_ENABLED=true "
                    "(la ejecución sin el motor corriendo es un no-op engañoso)."
                )
            if self.MOTOR_REST_EXECUTION_ENABLED and not self.MOTOR_REST_ENABLED:
                raise ValueError(
                    "MOTOR_REST_EXECUTION_ENABLED=true requiere MOTOR_REST_ENABLED=true "
                    "(la ejecución sin el motor corriendo es un no-op engañoso)."
                )
            # Motor 5 en PRODUCCIÓN (F3): requiere la LLAVE explícita (plan §5 — smoke
            # test + canonicalización + OK de Noel; el OK está documentado en el campo
            # MOTOR_MM_F3_ACK y en el commit que lo introdujo). Sin la llave, el flag
            # rompe el boot. NOTA: MOTOR_MM_ENABLED tampoco cuenta como "motor
            # habilitado" arriba — shadow no opera capital (misma regla que *_EXECUTION).
            if self.MOTOR_MM_EXECUTION_ENABLED and self.MOTOR_MM_F3_ACK != "NOEL-OK-F3":
                raise ValueError(
                    "MOTOR_MM_EXECUTION_ENABLED=true en PRODUCCIÓN sin la llave F3. "
                    "Secuencia (plan §5, el orden importa): 1) correr scripts/"
                    "motor5_smoke_test.py contra producción y verificar el cancel; "
                    "2) setear MOTOR_MM_F3_ACK='NOEL-OK-F3' en el env (acto deliberado "
                    "que documenta el OK); 3) redeploy con supervisión activa "
                    "(docs/motor5_runbook_activacion.md)."
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
