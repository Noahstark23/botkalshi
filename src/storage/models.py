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

    # Timestamps
    placed_at: datetime = Field(default_factory=_utc_now, index=True)
    filled_at: datetime | None = None
    settled_at: datetime | None = None

    notes: str | None = Field(default=None, max_length=500)


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
    gross_spread_cents: int | None = None  # spread BRUTO pre-comisión (para analizar cuánto come el fee)
    # Reconstrucción EXACTA del gate (agregadas 2026-06; NULL en ventanas pre-deploy).
    count: int | None = None       # contratos del opp detectado
    fees_cents: int | None = None  # comisión total estimada (ambas patas)
    edge_pct: float | None = None  # edge neto post-fee como % del capital comprometido
    leg_states: str | None = Field(default=None, max_length=50)  # "FILL/KILL", "FILL/ERROR_RED", etc.
    reconciled: bool = False
    kill_switch_fired: bool = False
    rollback_filled: bool = False
    cycle_latency_ms: int | None = None
    rest_rtt_ms: int | None = None
    created_at: datetime = Field(default_factory=_utc_now, index=True)

_engine: Any = None


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
    """
    cur = dbapi_conn.cursor()
    try:
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
            if column not in _existing_columns(conn, table):
                conn.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN "{column}" {col_type}')
                logger.info(f"migration: ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def init_db() -> None:
    """
    Crea tablas si no existen + aplica migraciones de ADD COLUMN.
    Idempotente - safe llamar múltiples veces.
    """
    engine = get_engine()
    SQLModel.metadata.create_all(engine)  # tablas/columnas nuevas en DBs nuevas
    apply_migrations(engine)              # columnas nuevas en tablas existentes


def get_session() -> Session:
    """Crear nueva sesión. Cerrar con context manager."""
    return Session(get_engine())
