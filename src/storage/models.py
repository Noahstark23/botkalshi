"""
Modelos de base de datos.

Schema:
    market_snapshots:  snapshots periódicos de markets (cada 5 min)
    orderbook_events:  eventos de orderbook_delta del WS (alta cardinalidad)
    trades:            registro de cada trade que el bot hace
    risk_events:       eventos del risk manager (pausas, kill-switches)
    daily_pnl:         resumen diario de PnL
    bot_runs:          tracking de cada arranque del bot
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import event
from sqlmodel import Field, Session, SQLModel, create_engine

from src.utils.config import get_settings


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _naive_utc_now() -> datetime:
    """UTC naive — convención de comparación del RiskManager/settlement (SQLite no
    preserva tz; mezclar aware/naive lanza TypeError al comparar). Motor 3 la usa para
    todos sus campos de tiempo (close_time / synced_at)."""
    return datetime.now(UTC).replace(tzinfo=None)


# =====================================================
# Tablas
# =====================================================


class MarketSnapshot(SQLModel, table=True):
    """Snapshot completo de un market en un momento."""

    __tablename__ = "market_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    ticker: str = Field(index=True, max_length=100)
    event_ticker: str = Field(index=True, max_length=100)
    yes_bid: int  # cents
    yes_ask: int
    no_bid: int
    no_ask: int
    last_price: int | None = None
    volume: int = 0
    open_interest: int = 0
    captured_at: datetime = Field(default_factory=_utc_now, index=True)


class OrderbookEvent(SQLModel, table=True):
    """Evento granular de orderbook_delta del WS."""

    __tablename__ = "orderbook_events"

    id: int | None = Field(default=None, primary_key=True)
    ticker: str = Field(index=True, max_length=100)
    side: str = Field(max_length=10)  # "yes" o "no"
    price_cents: int
    delta: int  # cambio en size, positivo o negativo
    received_at: datetime = Field(default_factory=_utc_now, index=True)


class Trade(SQLModel, table=True):
    """Registro de un trade del bot."""

    __tablename__ = "trades"

    id: int | None = Field(default=None, primary_key=True)
    client_order_id: str = Field(unique=True, max_length=100)
    ticker: str = Field(index=True, max_length=100)
    side: str = Field(max_length=10)  # yes/no
    action: str = Field(max_length=10)  # buy/sell
    count: int
    price_cents: int

    # Estrategia que generó el trade
    strategy: str = Field(index=True, max_length=50)
    estimated_edge_pct: float | None = None

    # Estado
    kalshi_order_id: str | None = None
    status: str = Field(default="pending", max_length=20)
    # pending → filled → settled (success path)
    # pending → cancelled (cancelado antes de fill)
    # pending → error (error en API)

    # Resultado financiero
    fill_price_cents: int | None = None
    fees_cents: int | None = None
    pnl_cents: int | None = None

    # Motor 3 (CLV): la posición se cerró con un SELL anticipado de Motor 3, NO por
    # resolución del mercado. El SettlementPoller SALTEA estas filas → no doble-cuenta
    # el PnL (la pérdida/ganancia ya quedó realizada al precio de salida).
    closed_by_clv: bool = False

    # Motor 5 F2 (fills PARCIALES de órdenes resting): contratos realmente llenados.
    # None = semántica legacy (orden inmediata FOK/IOC: count entero se llenó o nada).
    # El RiskManager usa filled_count (si está) para la exposición de filas 'filled':
    # una resting de 1000 con 500 llenados (resto cancelado) expone 500, no 1000.
    filled_count: int | None = None

    # Timestamps
    placed_at: datetime = Field(default_factory=_utc_now, index=True)
    filled_at: datetime | None = None
    settled_at: datetime | None = None

    notes: str | None = Field(default=None, max_length=500)


class PortfolioPosition(SQLModel, table=True):
    """
    Cache de una posición ABIERTA de Kalshi — el estado real de la cuenta para el Motor 3
    (CLV). Una fila por market con posición neta != 0. El PortfolioPoller la sincroniza
    cada 60s desde GET /portfolio/positions, cruzando get_market() para el `close_time`
    (el endpoint de posiciones no lo trae). Read-only respecto al capital: solo cachea.

    Convención de tiempo: NAIVE UTC en todos los campos (close_time/synced_at) para poder
    comparar con `datetime.now(UTC).replace(tzinfo=None)` sin TypeError.
    """

    __tablename__ = "portfolio_positions"

    id: int | None = Field(default=None, primary_key=True)
    ticker: str = Field(unique=True, index=True, max_length=100)
    side: str = Field(max_length=10)  # "yes" | "no" — lado neto que tenemos abierto
    count: int  # contratos (abs de la posición neta de Kalshi)
    exposure_cents: int | None = None  # market_exposure de Kalshi (capital en riesgo)
    close_time: datetime | None = None  # cierre del mercado (NAIVE UTC) — gate del CLV
    synced_at: datetime = Field(default_factory=_naive_utc_now, index=True)
    # FASE 2 (trailing stop): máximo bid observado del lado abierto desde el entry. Lo persiste
    # el engine entre ticks; el trailing cierra si el bid retrocede drop_cents desde acá.
    peak_bid_cents: int | None = None


class RiskEvent(SQLModel, table=True):
    """Eventos del risk manager."""

    __tablename__ = "risk_events"

    id: int | None = Field(default=None, primary_key=True)
    event_type: str = Field(index=True, max_length=50)
    # daily_loss_limit, weekly_loss_limit, monthly_loss_limit,
    # exposure_limit, kill_switch, manual_pause, etc.

    severity: str = Field(max_length=20)  # info, warning, critical
    message: str = Field(max_length=1000)
    capital_at_event: float | None = None
    triggered_at: datetime = Field(default_factory=_utc_now, index=True)


class DailyPnL(SQLModel, table=True):
    """Resumen diario de PnL."""

    __tablename__ = "daily_pnl"

    id: int | None = Field(default=None, primary_key=True)
    date: str = Field(unique=True, index=True, max_length=10)  # YYYY-MM-DD
    starting_capital: float
    ending_capital: float
    pnl: float
    pnl_pct: float
    trades_count: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    strategy_breakdown: str | None = None  # JSON string


class BotRun(SQLModel, table=True):
    """
    Tracking de cada arranque del bot.
    Útil para debugging crashes y auditoría.
    """

    __tablename__ = "bot_runs"

    id: int | None = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=_utc_now, index=True)
    ended_at: datetime | None = None
    environment: str = Field(max_length=20)  # demo / production
    trading_enabled: bool = False
    motors_enabled: str = ""  # JSON list
    capital_at_start: float
    version: str = Field(max_length=20, default="0.1.0")
    crash_reason: str | None = Field(default=None, max_length=500)


class EdgeWindow(SQLModel, table=True):
    """
    Ventana de edge detectada por el Motor REST (shadow + live).

    Mide la captura NETA real: cada vez que el trigger del Motor REST dispara,
    se registra la ventana — spread detectado, latencias, estados de las patas y
    resultado de la ejecución/rollback. Es solo un registro de datos; no dispara
    lógica de trading. Ver docs/motor_rest_design.md §5.
    """

    __tablename__ = "edge_windows"

    id: int | None = Field(default=None, primary_key=True)
    market_ticker: str = Field(index=True, max_length=100)
    duration_ms: int | None = None
    magnitude_cents: int  # edge NETO post-comisión (lo que decide)
    gross_spread_cents: int | None = (
        None  # spread BRUTO pre-comisión (para analizar cuánto come el fee)
    )
    # Reconstrucción EXACTA del gate (agregadas 2026-06; NULL en ventanas pre-deploy).
    count: int | None = None  # contratos del opp detectado
    fees_cents: int | None = None  # comisión total estimada (ambas patas)
    # OJO — COLUMNA POLIMÓRFICA: el nombre solo describe a los kinds binarios.
    #   binary / multi_outcome (M1, REST) → edge neto post-fee, en % del capital
    #   ofi (M8)                          → Z-SCORE de la señal (adimensional)
    #   spillover / spillover_exec (M9)   → move del trigger, en CENTAVOS
    # No se renombra para no reescribir las filas históricas; el mapa autoritativo de
    # unidades es `_EDGE_UNITS` (monitoring/health.py) y /stats/edges declara la unidad
    # de cada bloque. Todo consumidor nuevo DEBE mirar `kind` antes de interpretar este
    # número (incidente 2026-07-28: buckets en pp aplicados a z-scores → "max 2678.83pp"
    # y 1349 falsos "sospechosos >8pp").
    edge_pct: float | None = None
    # Tipo de detección: "binary" (mismo market, libro cruzado) | "multi_outcome"
    # (1X2/winner: N markets del mismo evento sumando <100). NULL = pre-P3 (binary).
    kind: str | None = Field(default=None, max_length=20)
    leg_states: str | None = Field(
        default=None, max_length=50
    )  # "FILL/KILL", "FILL/ERROR_RED", etc.
    reconciled: bool = False
    kill_switch_fired: bool = False
    rollback_filled: bool = False
    cycle_latency_ms: int | None = None
    rest_rtt_ms: int | None = None
    created_at: datetime = Field(default_factory=_utc_now, index=True)


class OperationalState(SQLModel, table=True):
    """
    Estado operativo PERSISTENTE (sobrevive restarts del contenedor).

    Motivación: `BotState.is_paused` vive solo en memoria. Con Coolify
    `restart: unless-stopped`, un kill-switch a las 3am + cualquier reinicio (OOM,
    deploy, hiccup del VPS) borraría la pausa y el bot volvería a operar solo. Esta
    tabla guarda el estado en DB para rehidratarlo al boot. key/value genérico.
    """

    __tablename__ = "operational_state"

    key: str = Field(primary_key=True, max_length=50)
    value: str = Field(max_length=20)  # p.ej. "engaged" / "clear"
    reason: str | None = Field(default=None, max_length=500)
    updated_at: datetime = Field(default_factory=_utc_now)


class Motor2FunnelSnapshot(SQLModel, table=True):
    """
    Foto del embudo de Motor 2 por ciclo (Loop Engineering — memoria del diagnóstico).

    El embudo (matched / reject_* / best_edge) vivía SOLO en logs efímeros → no se podía
    trendear. El poller persiste una fila por ciclo (con odds REALES) para que el Analyst
    Loop agregue la ventana y distinga "mercado eficiente" de "bug de matching" en el tiempo.
    """

    __tablename__ = "motor2_funnel_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    odds_total: int = 0
    started_skip: int = 0
    kalshi_total: int = 0
    events_matched: int = 0
    reject_absent: int = 0
    reject_cardinality: int = 0
    reject_names: int = 0
    reject_no_fair: int = 0
    best_net_edge_pp: float = -1.0  # mejor edge neto visto (pp); -1 = nada evaluado
    signals: int = 0  # señales emitidas ese ciclo
    created_at: datetime = Field(default_factory=_utc_now, index=True)


class MMQuote(SQLModel, table=True):
    """
    Quote SHADOW emitida por el Motor 5 en un tick (F1 — cero órdenes, registro puro).

    Es el tracker del gate F1→F2 (≥14 días): qué se cotizó, alrededor de qué fair, con
    qué book en ese instante. bid/ask None = lado retirado por tope de inventario.
    """

    __tablename__ = "mm_quotes"

    id: int | None = Field(default=None, primary_key=True)
    ticker: str = Field(index=True, max_length=100)
    fair_prob: float
    fair_age_sec: float = 0.0  # edad del fair al cotizar (mide staleness del canal M2)
    bid_cents: int | None = None
    ask_cents: int | None = None
    size: int = 0
    yes_bid: int | None = None  # top-of-book al momento de cotizar (contexto del quote)
    yes_ask: int | None = None
    inventory: int = 0  # net simulado ANTES del quote (explica el skew aplicado)
    created_at: datetime = Field(default_factory=_utc_now, index=True)


class MMShadowFill(SQLModel, table=True):
    """
    Fill HIPOTÉTICO del Motor 5 (F1): el book cruzó estrictamente una quote resting del
    tick anterior. `rule` guarda la evidencia del cruce (auditable en la revisión
    adversarial del gate F1→F2).
    """

    __tablename__ = "mm_shadow_fills"

    id: int | None = Field(default=None, primary_key=True)
    ticker: str = Field(index=True, max_length=100)
    side: str = Field(max_length=4)  # "buy" | "sell"
    price_cents: int
    count: int
    fee_cents: int = 0  # comisión real del fill (kalshi_fee_cents con count)
    rule: str = Field(max_length=50)  # p.ej. "ask 38 < bid 40"
    inventory_after: int = 0
    # Fair-at-fill (2026-08-07): la pregunta central del A/B de M5 es si el fill capturó
    # spread o fue selección adversa (el fair se movió y el mercado nos cruzó). Sin el
    # fair DEL MOMENTO del fill era incontestable: 66 fills históricos sin el dato.
    # Nullable: las filas viejas quedan NULL y el análisis las excluye.
    quote_fair_prob: float | None = None  # fair sobre el que se construyó la quote (tick t−1)
    fill_fair_prob: float | None = None  # fair vigente al detectar el cruce (tick t)
    yes_bid: int | None = None  # top-of-book YES en el tick del fill
    yes_ask: int | None = None
    created_at: datetime = Field(default_factory=_utc_now, index=True)


class MMFunnelSnapshot(SQLModel, table=True):
    """
    Foto del embudo del Motor 5 por tick (patrón Motor2FunnelSnapshot): cuántos tickers
    tenían fair fresco, cuántos se cotizaron, skips por causa, fills e inventario/PnL.
    Es la memoria del loop de ingeniería del plan §6.
    """

    __tablename__ = "mm_funnel_snapshots"

    id: int | None = Field(default=None, primary_key=True)
    fair_fresh: int = 0  # tickers con fair dentro del TTL
    quoted: int = 0
    skip_no_book: int = 0
    skip_unprofitable: int = 0
    skip_degenerate: int = 0
    skip_fair_range: int = 0
    fills: int = 0
    inventory_abs: int = 0  # Σ|net| simulado
    mtm_pnl_cents: int = 0  # PnL mark-to-market neto de fees (el número del gate)
    created_at: datetime = Field(default_factory=_utc_now, index=True)


class AnalystVerdict(SQLModel, table=True):
    """
    Veredicto diario del Analyst Loop (memoria persistente que se compara día-a-día).

    ADVISORY-ONLY: registro de análisis; NO gatea trading. Lo escribe AnalystLoop tras
    agregar EdgeWindow(consensus) + Motor2FunnelSnapshot de la ventana.
    """

    __tablename__ = "analyst_verdicts"

    id: int | None = Field(default=None, primary_key=True)
    window_hours: int = 24
    consensus_total: int = 0  # filas EdgeWindow(kind=consensus) en la ventana
    consensus_gt_thresh: int = 0  # cuántas superan el umbral (edges accionables)
    best_edge_pp: float = -1.0
    mean_edge_pp: float = 0.0
    funnel_cycles: int = 0  # ciclos del embudo en la ventana (0 = sin datos)
    matched: int = 0
    reject_absent: int = 0
    reject_cardinality: int = 0
    reject_names: int = 0
    reject_no_fair: int = 0
    verdict: str = Field(max_length=30)  # eficiente / matching_bug / edge_candidato / ...
    recommendation: str | None = Field(default=None, max_length=500)
    recorded_at: datetime = Field(default_factory=_utc_now, index=True)


_engine: Any = None

# Clave del kill-switch (Motor REST + RiskManager) en operational_state.
_KILL_SWITCH_KEY = "kill_switch"


def _apply_sqlite_pragmas(dbapi_conn: Any, _connection_record: Any) -> None:
    """
    PRAGMAs por-conexión para SQLite, aplicados en cada connect del pool.

    - journal_mode=WAL: permite lectores concurrentes + 1 escritor sin el lock
      EXCLUSIVO del modo 'delete' (default). Necesario porque V1 (data_capture) y
      el Motor REST shadow escriben en la MISMA DB; con 'delete' los escritores se
      serializan duro y, bajo carga (soccer en vivo), aparece 'database is locked'.
      WAL es persistente (la primera conexión convierte la DB), pero re-aplicarlo
      es idempotente y barato.
    - busy_timeout=5000: ante lock, esperar hasta 5s en vez de fallar al instante
      (el default es 0 → fallo inmediato). Cubre el residual de contención.
    - auto_vacuum=INCREMENTAL (incidente disco-lleno 2026-07-10): SOLO tiene efecto en una
      DB VACÍA (antes de crear tablas), y persiste en el archivo. En una DB ya existente
      con auto_vacuum=NONE (todas las de prod hasta hoy) es un NO-OP inofensivo — cambiarlo
      ahí requiere un full VACUUM (o el rebuild_db.py, que crea la nueva ya con INCREMENTAL).
      El efecto real es que TODA DB nueva (deploy fresco) nace con auto_vacuum → el
      incremental_vacuum del loop de mantenimiento le recupera disco online, sin full VACUUM
      nunca. Se setea PRIMERO, antes de WAL, porque exige que aún no haya ninguna tabla.
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA auto_vacuum=INCREMENTAL")  # antes de crear tablas (no-op si ya hay)
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")  # seguro con WAL; menos fsync por commit
    finally:
        cur.close()


def get_engine() -> Any:
    """Singleton del engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        is_sqlite = "sqlite" in settings.DATABASE_URL
        if is_sqlite:
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.DATABASE_URL,
            echo=False,
            connect_args=connect_args,
        )
        if is_sqlite:
            # WAL + busy_timeout en cada conexión (evita 'database is locked' con
            # múltiples escritores: V1 captura + Motor REST shadow).
            event.listen(_engine, "connect", _apply_sqlite_pragmas)
    return _engine


# Migraciones de ADD COLUMN. SQLite no tiene historia de migración y create_all NO altera
# tablas existentes → estas cubren columnas agregadas a tablas que ya existen en la DB
# productiva. ADD COLUMN nullable es metadata-only en SQLite (instantáneo aun en DBs grandes).
# Cada entrada: (tabla, columna, tipo SQL). Idempotente: solo agrega si falta.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("edge_windows", "count", "INTEGER"),
    ("edge_windows", "fees_cents", "INTEGER"),
    ("edge_windows", "edge_pct", "FLOAT"),
    ("edge_windows", "kind", "VARCHAR(20)"),  # P3: binary | multi_outcome
    ("trades", "closed_by_clv", "BOOLEAN DEFAULT 0"),  # Motor 3: cierre anticipado CLV
    ("portfolio_positions", "peak_bid_cents", "INTEGER"),  # Motor 3 FASE 2: trailing stop
    ("trades", "filled_count", "INTEGER"),  # Motor 5 F2: fills parciales de resting orders
    # Motor 5 fair-at-fill: sin el fair del momento del fill, el A/B no puede separar
    # spread capturado de selección adversa (el dato que faltaba para graduar/archivar).
    ("mm_shadow_fills", "quote_fair_prob", "FLOAT"),
    ("mm_shadow_fills", "fill_fair_prob", "FLOAT"),
    ("mm_shadow_fills", "yes_bid", "INTEGER"),
    ("mm_shadow_fills", "yes_ask", "INTEGER"),
]


def _existing_columns(conn: Any, table: str) -> set[str]:
    """Nombres de columna actuales de una tabla SQLite (vía PRAGMA table_info)."""
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}  # r = (cid, name, type, notnull, dflt, pk)


def apply_migrations(engine: Any) -> None:
    """
    Aplica los ADD COLUMN pendientes, idempotente. Convive con create_all: este último
    crea tablas/columnas en DBs NUEVAS (donde no hace falta migrar); esto cubre las
    columnas nuevas en tablas EXISTENTES (donde create_all no hace nada). Solo SQLite;
    en otro engine sería Alembic.
    """
    if "sqlite" not in get_settings().DATABASE_URL:
        return
    with engine.begin() as conn:
        for table, column, col_type in _MIGRATIONS:
            existing = _existing_columns(conn, table)
            if not existing:
                continue  # la tabla no existe en esta DB → create_all la creará con el schema actual
            if column not in existing:
                conn.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN "{column}" {col_type}')
                logger.info(f"migration: ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db() -> None:
    """
    Crea tablas si no existen + aplica migraciones de ADD COLUMN.
    Idempotente - safe llamar múltiples veces.
    """
    engine = get_engine()
    SQLModel.metadata.create_all(engine)  # tablas/columnas nuevas en DBs nuevas
    apply_migrations(engine)  # columnas nuevas en tablas existentes


def get_session() -> Session:
    """Crear nueva sesión. Cerrar con context manager."""
    return Session(get_engine())


# =====================================================
# Kill-switch persistente (sobrevive restarts de Coolify)
# =====================================================


def engage_kill_switch(reason: str) -> None:
    """
    Marca el kill-switch como ENGAGED en DB (idempotente, upsert).

    Lo llaman AMBOS kill-switches (RestExecutor._fire_kill_switch y
    RiskManager._trigger_kill_switch). El caller la envuelve en try/except: un fallo
    de persistencia NO debe tirar el path del kill-switch (prioridad: alertar al humano),
    pero se loguea CRITICAL — si no persiste, un restart re-introduce el gap.
    """
    with get_session() as s:
        row = s.get(OperationalState, _KILL_SWITCH_KEY)
        if row is None:
            row = OperationalState(key=_KILL_SWITCH_KEY, value="engaged", reason=reason[:500])
        else:
            row.value = "engaged"
            row.reason = reason[:500]
            row.updated_at = _utc_now()
        s.add(row)
        s.commit()


def kill_switch_engaged() -> tuple[bool, str | None]:
    """(engaged, reason) del kill-switch persistente. Propaga si la DB falla — el
    caller decide el fail-safe (la Capa A asume engaged ante error)."""
    with get_session() as s:
        row = s.get(OperationalState, _KILL_SWITCH_KEY)
        if row is None or row.value != "engaged":
            return False, None
        return True, row.reason


_MM_QUOTES_PAUSED_KEY = "mm_quotes_paused"


def set_mm_quotes_paused(paused: bool, reason: str | None = None) -> None:
    """
    Modo quotes_paused del Motor 5 (F2, plan §4.2): el MM DEJA de emitir quotes nuevas
    pero SIGUE gestionando lo abierto (reconcile + cancel). Es la pausa intermedia entre
    "operar normal" y el kill-switch (que cancela todo y frena el motor). Persistente
    (sobrevive restarts), patrón kill-switch.
    """
    with get_session() as s:
        row = s.get(OperationalState, _MM_QUOTES_PAUSED_KEY)
        value = "paused" if paused else "clear"
        if row is None:
            row = OperationalState(
                key=_MM_QUOTES_PAUSED_KEY, value=value, reason=(reason or "")[:500] or None
            )
        else:
            row.value = value
            row.reason = (reason or "")[:500] or None
            row.updated_at = _utc_now()
        s.add(row)
        s.commit()


def mm_quotes_paused() -> tuple[bool, str | None]:
    """(paused, reason). Propaga si la DB falla — el engine asume paused ante error
    (fail-safe: no cotizar es el estado seguro)."""
    with get_session() as s:
        row = s.get(OperationalState, _MM_QUOTES_PAUSED_KEY)
        if row is None or row.value != "paused":
            return False, None
        return True, row.reason


def clear_kill_switch() -> None:
    """Despausa el kill-switch persistente. SOLO desde scripts/clear_kill_switch.py
    (que verifica posiciones en cero antes de llamar). Nada automático la limpia."""
    with get_session() as s:
        row = s.get(OperationalState, _KILL_SWITCH_KEY)
        if row is not None:
            row.value = "clear"
            row.reason = None
            row.updated_at = _utc_now()
            s.add(row)
            s.commit()
