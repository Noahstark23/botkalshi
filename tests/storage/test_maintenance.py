"""
Retención de tablas de diagnóstico (incidente disco-lleno 2026-07-10).

Verifica: se borran filas más viejas que la ventana; se conservan las recientes; y las
tablas de ESTADO DE TRADING (trades, risk_events, …) JAMÁS se tocan.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import SQLModel, create_engine, select

import src.storage.models as _models
from src.storage.maintenance import _RETENTION_DAYS, prune_diagnostics
from src.storage.models import (
    EdgeWindow,
    MMQuote,
    OrderbookEvent,
    RiskEvent,
    Trade,
    get_session,
)

NOW = datetime(2026, 7, 10, 12, 0)  # naive UTC


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(_models, "_engine", engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    monkeypatch.setattr(_models, "_engine", None)


def _seed():
    old = NOW - timedelta(days=40)  # más viejo que TODAS las ventanas
    recent = NOW - timedelta(hours=1)
    with get_session() as s:
        # Diagnóstico: uno viejo + uno reciente por tabla representativa.
        s.add(OrderbookEvent(ticker="T", side="yes", price_cents=40, delta=1, received_at=old))
        s.add(OrderbookEvent(ticker="T", side="yes", price_cents=40, delta=1, received_at=recent))
        s.add(
            MMQuote(ticker="T", fair_prob=0.5, bid_cents=40, ask_cents=45, size=1, created_at=old)
        )
        s.add(
            MMQuote(
                ticker="T", fair_prob=0.5, bid_cents=40, ask_cents=45, size=1, created_at=recent
            )
        )
        s.add(EdgeWindow(kind="consensus", market_ticker="T", magnitude_cents=1, created_at=old))
        s.add(EdgeWindow(kind="consensus", market_ticker="T", magnitude_cents=1, created_at=recent))
        # ESTADO DE TRADING (sagrado): viejísimo, NUNCA debe borrarse.
        s.add(
            Trade(
                client_order_id="sagrado",
                ticker="T",
                side="yes",
                action="buy",
                count=1,
                price_cents=40,
                strategy="motor_1_arbitrage",
                status="settled",
                placed_at=old,
            )
        )
        s.add(
            RiskEvent(
                event_type="atomic_rollback", severity="warning", message="x", triggered_at=old
            )
        )
        s.commit()


def _count(model) -> int:
    with get_session() as s:
        return len(list(s.exec(select(model))))


def test_prune_deletes_old_keeps_recent():
    _seed()
    deleted = prune_diagnostics(now=NOW)

    # Cada tabla de diagnóstico: la vieja se borró (1), la reciente quedó.
    assert deleted["orderbook_events"] == 1
    assert deleted["mm_quotes"] == 1
    assert deleted["edge_windows"] == 1
    assert _count(OrderbookEvent) == 1  # solo la reciente
    assert _count(MMQuote) == 1
    assert _count(EdgeWindow) == 1


def test_prune_never_touches_trading_state():
    """La regla dura: trades y risk_events (estado de trading) JAMÁS se podan, por viejos
    que sean. No están en _RETENTION_DAYS y prune no debe rozarlos."""
    _seed()
    prune_diagnostics(now=NOW)
    assert _count(Trade) == 1  # el trade 'sagrado' viejísimo sigue
    assert _count(RiskEvent) == 1
    assert "trades" not in _RETENTION_DAYS
    assert "risk_events" not in _RETENTION_DAYS
    assert "operational_state" not in _RETENTION_DAYS  # kill-switch — intocable


def test_edge_windows_retained_longer_than_mm():
    """edge_windows retiene 30d (sustrato de análisis); mm_quotes solo 7 → una fila de 10 días
    sobrevive en edge_windows pero NO en mm_quotes."""
    ten_days = NOW - timedelta(days=10)
    with get_session() as s:
        s.add(
            EdgeWindow(kind="consensus", market_ticker="T", magnitude_cents=1, created_at=ten_days)
        )
        s.add(
            MMQuote(
                ticker="T", fair_prob=0.5, bid_cents=40, ask_cents=45, size=1, created_at=ten_days
            )
        )
        s.commit()
    prune_diagnostics(now=NOW)
    assert _count(EdgeWindow) == 1  # 10 días < 30 → se conserva
    assert _count(MMQuote) == 0  # 10 días > 7 → se borra


def test_missing_table_does_not_break_others(monkeypatch):
    """Best-effort: si una tabla del mapa no existe (renombrada), las demás igual se podan."""
    _seed()
    monkeypatch.setitem(_RETENTION_DAYS, "tabla_inexistente", ("created_at", 1))
    deleted = prune_diagnostics(now=NOW)
    assert deleted["tabla_inexistente"] == -1  # falló, marcada
    assert deleted["orderbook_events"] == 1  # las demás siguieron
