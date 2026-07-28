"""
Tests de /stats/daily y /stats/edges (observabilidad HTTP read-only).

Patrón portado de Polybot: continuidad de captura y distribución de edges por
kind consultables por GET, sin terminal ni SQL (los consume el agente web).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

import src.storage.models as _models
from src.monitoring.health import app
from src.storage.models import (
    AnalystVerdict,
    EdgeWindow,
    MarketSnapshot,
    Motor2FunnelSnapshot,
    OrderbookEvent,
    get_session,
)


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    # StaticPool: una única conexión compartida — el TestClient corre en otro
    # thread y con el pool default cada conexión nueva a :memory: es una DB vacía
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(_models, "_engine", engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    monkeypatch.setattr(_models, "_engine", None)


def _naive(days_ago: float = 0) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago)


def _seed() -> None:
    with get_session() as s:
        # hoy: 2 snapshots, 1 evento, 3 edge_windows (2 ofi con edge, 1 legacy NULL)
        for _ in range(2):
            s.add(
                MarketSnapshot(
                    ticker="KXT-1",
                    event_ticker="KXT",
                    yes_bid=40,
                    yes_ask=42,
                    no_bid=58,
                    no_ask=60,
                    captured_at=_naive(0),
                )
            )
        s.add(
            OrderbookEvent(
                ticker="KXT-1", side="yes", price_cents=40, delta=1, received_at=_naive(0)
            )
        )
        s.add(
            EdgeWindow(
                market_ticker="KXT-1",
                magnitude_cents=3,
                kind="ofi",
                edge_pct=3.2,
                created_at=_naive(0),
            )
        )
        s.add(
            EdgeWindow(
                market_ticker="KXT-2",
                magnitude_cents=1,
                kind="ofi",
                edge_pct=0.5,
                created_at=_naive(0),
            )
        )
        s.add(
            EdgeWindow(  # legacy pre-P3: kind NULL, edge_pct NULL
                market_ticker="KXT-3",
                magnitude_cents=2,
                created_at=_naive(0),
            )
        )
        s.add(Motor2FunnelSnapshot(events_matched=5, signals=1, created_at=_naive(0)))
        s.add(AnalystVerdict(verdict="eficiente", recorded_at=_naive(0)))
        # hace 2 días: solo 1 snapshot (ayer queda como agujero visible)
        s.add(
            MarketSnapshot(
                ticker="KXT-1",
                event_ticker="KXT",
                yes_bid=40,
                yes_ask=42,
                no_bid=58,
                no_ask=60,
                captured_at=_naive(2),
            )
        )
        s.commit()  # get_session() de este repo NO auto-commitea


def test_stats_daily_conteos_y_agujero():
    _seed()
    with TestClient(app) as client:
        body = client.get("/stats/daily").json()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    assert body["daily"][today]["market_snapshots"] == 2
    assert body["daily"][today]["orderbook_events"] == 1
    assert body["daily"][today]["edge_windows_total"] == 3
    assert body["daily"][today]["edges_ofi"] == 2
    assert body["daily"][today]["edges_binary"] == 1  # kind NULL -> binary
    assert body["daily"][today]["m2_funnel_cycles"] == 1
    assert body["daily"][today]["m2_signals"] == 1
    assert body["daily"][today]["analyst_verdicts"] == 1
    # dia sin captura NO aparece: ese es el agujero, visible a simple vista
    assert yesterday not in body["daily"]


def test_stats_edges_distribucion_por_kind():
    _seed()
    with TestClient(app) as client:
        body = client.get("/stats/edges").json()
    ofi = body["by_kind"]["ofi"]
    assert ofi["rows_total"] == 2
    assert ofi["gt_1pp"] == 1  # solo el de 3.2pp
    assert ofi["gt_3pp"] == 1
    assert ofi["gt_8pp_sospechosos"] == 0
    assert ofi["edge_pct_max"] == pytest.approx(3.2)
    legacy = body["by_kind"]["binary"]
    assert legacy["rows_total"] == 1
    assert legacy["rows_with_edge_pct"] == 0  # NULL excluido de buckets
    assert body["top_10_edges"][0]["edge_pct"] == pytest.approx(3.2)
    assert body["top_10_edges"][0]["kind"] == "ofi"


def test_param_days_clamp_fastapi():
    _seed()
    with TestClient(app) as client:
        assert client.get("/stats/daily", params={"days": 5000}).status_code == 422
        assert client.get("/stats/daily", params={"days": 7}).json()["days_requested"] == 7


def test_db_vacia_no_rompe():
    with TestClient(app) as client:
        daily = client.get("/stats/daily").json()
        edges = client.get("/stats/edges").json()
    assert daily["daily"] == {}
    assert edges["by_kind"] == {}
    assert edges["top_10_edges"] == []
