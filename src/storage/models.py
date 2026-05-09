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


# =====================================================
# Engine y session
# =====================================================

_engine: Any = None


def get_engine() -> Any:
    """Singleton del engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        if "sqlite" in settings.DATABASE_URL:
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.DATABASE_URL,
            echo=False,
            connect_args=connect_args,
        )
    return _engine


def init_db() -> None:
    """
    Crea tablas si no existen.
    Idempotente - safe llamar múltiples veces.
    """
    engine = get_engine()
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    """Crear nueva sesión. Cerrar con context manager."""
    return Session(get_engine())
