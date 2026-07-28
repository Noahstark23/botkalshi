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
    Trade,
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
    assert ofi["value_max"] == pytest.approx(3.2)
    legacy = body["by_kind"]["binary"]
    assert legacy["rows_total"] == 1
    assert legacy["rows_with_value"] == 0  # NULL excluido de buckets


class TestEdgesUnidadPorKind:
    """
    Incidente 2026-07-28: `/stats/edges` aplicaba buckets en PUNTOS PORCENTUALES a
    una columna polimórfica. M8 guarda z-scores en `edge_pct` y M9 centavos → el
    reporte mostraba "max 2678.83pp" y 1349 filas "sospechosas >8pp" que eran
    z-scores normales, y los contadores >0/>1/>3 idénticos (el detector solo emite
    con |z| >= z_min, así que no existe señal entre 0 y el umbral). Ese hueco se
    leyó como artefacto de datos; era la unidad equivocada.
    """

    def test_ofi_no_reporta_buckets_en_pp(self):
        _seed()
        with TestClient(app) as client:
            ofi = client.get("/stats/edges").json()["by_kind"]["ofi"]
        assert ofi["unit"] == "zscore"
        # Los buckets en pp NO se calculan para una serie que no está en %
        assert "gt_8pp_sospechosos" not in ofi
        assert "gt_3pp" not in ofi
        assert "nota_unidad" in ofi

    def test_binarios_si_reportan_buckets_en_pp(self):
        with get_session() as s:
            s.add(EdgeWindow(market_ticker="K-1", magnitude_cents=3, kind="binary", edge_pct=9.5))
            s.add(EdgeWindow(market_ticker="K-2", magnitude_cents=1, kind="binary", edge_pct=2.0))
            s.commit()
        with TestClient(app) as client:
            b = client.get("/stats/edges").json()["by_kind"]["binary"]
        assert b["unit"] == "pct"
        assert b["gt_1pp"] == 2
        assert b["gt_3pp"] == 1
        assert b["gt_8pp_sospechosos"] == 1  # el guardarraíl SOLO tiene sentido acá

    def test_top_10_excluye_unidades_que_no_son_pct(self):
        """Un z-score de 40 no puede encabezar un ranking de 'edges'."""
        with get_session() as s:
            s.add(EdgeWindow(market_ticker="K-OFI", magnitude_cents=1, kind="ofi", edge_pct=40.0))
            s.add(EdgeWindow(market_ticker="K-BIN", magnitude_cents=3, kind="binary", edge_pct=4.0))
            s.commit()
        with TestClient(app) as client:
            top = client.get("/stats/edges").json()["top_10_edges"]
        assert [r["ticker"] for r in top] == ["K-BIN"]


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


def _settled_trade(strategy: str, pnl_cents: int, days_ago: int = 0, oid: str = "x") -> Trade:
    return Trade(
        client_order_id=f"{strategy}-{oid}-{pnl_cents}-{days_ago}",
        ticker="KXT-1",
        side="yes",
        action="buy",
        count=1,
        price_cents=50,
        strategy=strategy,
        status="settled",
        pnl_cents=pnl_cents,
        fees_cents=1,
        settled_at=_naive(days_ago),
    )


def test_stats_motors_desglosa_pnl_por_motor():
    """El neto enmascara: un motor que sangra junto a otro positivo."""
    with get_session() as s:
        s.add(_settled_trade("motor_2_consensus", -4000, oid="a"))  # -$40
        s.add(_settled_trade("motor_2_consensus", -1000, oid="b"))  # -$10
        s.add(_settled_trade("motor_1_arbitrage", 300, oid="c"))  # +$3
        s.commit()
    with TestClient(app) as client:
        body = client.get("/stats/motors").json()

    assert body["net_pnl_usd"] == pytest.approx(-47.0)
    m2 = body["by_motor"]["motor_2_consensus"]
    assert m2["pnl_usd"] == pytest.approx(-50.0)
    assert m2["settled_trades"] == 2
    assert m2["losses"] == 2
    assert m2["win_rate_pct"] == 0.0
    assert m2["verdict_hint"] == "sangra"
    m1 = body["by_motor"]["motor_1_arbitrage"]
    assert m1["pnl_usd"] == pytest.approx(3.0)
    assert m1["verdict_hint"] == "positivo"
    # ordenado del peor al mejor: el que sangra primero
    assert next(iter(body["by_motor"])) == "motor_2_consensus"


def test_stats_motors_detecta_ruido_y_peor_dia():
    with get_session() as s:
        s.add(_settled_trade("motor_1_arbitrage", 5, days_ago=0, oid="d"))  # +$0.05
        s.add(_settled_trade("motor_1_arbitrage", -3, days_ago=2, oid="e"))  # -$0.03
        s.commit()
    with TestClient(app) as client:
        body = client.get("/stats/motors").json()
    m1 = body["by_motor"]["motor_1_arbitrage"]
    assert m1["verdict_hint"].startswith("ruido")
    two_ago = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%d")
    assert m1["worst_day"]["date"] == two_ago


class TestFeeCoverageEnStatsMotors:
    """
    Incidente 2026-07-28: M1 reportaba `fees_usd: 0.00` sobre 519 trades settleados
    porque nadie persistía `fees_cents`. El criterio de cierre del mes de prueba
    ("PnL/trade > 2 × fee promedio") comparaba contra CERO → aprobaba exactamente al
    motor que estaba diseñado para descartar. El endpoint tiene que DECIR que el dato
    falta, no rellenar el hueco con un absoluto.
    """

    def test_expone_cuantos_trades_no_tienen_fee(self):
        with get_session() as s:
            t = _settled_trade("motor_1_arbitrage", 10, oid="nofee")
            t.fees_cents = None
            s.add(t)
            s.add(_settled_trade("motor_1_arbitrage", 10, oid="confee"))
            s.commit()
        with TestClient(app) as client:
            m1 = client.get("/stats/motors").json()["by_motor"]["motor_1_arbitrage"]
        assert m1["fees_missing_trades"] == 1
        assert m1["fees_coverage_pct"] == 50.0
        assert m1["verdict_hint"].startswith("indeterminado")

    def test_umbral_de_ruido_es_relativo_al_fee(self):
        """+$0.10/trade con fee $0.20 es ruido aunque el PnL neto sea positivo."""
        with get_session() as s:
            for i in range(3):
                t = _settled_trade("motor_1_arbitrage", 10, oid=f"r{i}")
                t.fees_cents = 20  # $0.20 de fee por trade → umbral $0.40
                s.add(t)
            s.commit()
        with TestClient(app) as client:
            m1 = client.get("/stats/motors").json()["by_motor"]["motor_1_arbitrage"]
        assert m1["fees_coverage_pct"] == 100.0
        assert m1["fee_per_trade_usd"] == pytest.approx(0.20)
        assert m1["ruido_umbral_usd"] == pytest.approx(0.40)
        assert m1["verdict_hint"].startswith("ruido")

    def test_fee_cero_real_no_hace_el_umbral_cero(self):
        """Piso anti-degenerado: sin él, fee=0 aprobaría +$0.01/trade como 'positivo'."""
        with get_session() as s:
            for i in range(3):
                t = _settled_trade("motor_5_mm", 1, oid=f"z{i}")
                t.fees_cents = 0
                s.add(t)
            s.commit()
        with TestClient(app) as client:
            m5 = client.get("/stats/motors").json()["by_motor"]["motor_5_mm"]
        assert m5["ruido_umbral_usd"] == pytest.approx(0.15)
        assert m5["verdict_hint"].startswith("ruido")


def test_stats_motors_ignora_no_settleados():
    with get_session() as s:
        t = _settled_trade("motor_rest_arb", -5000, oid="f")
        t.settled_at = None  # placed pero sin settlear
        t.status = "filled"
        s.add(t)
        s.commit()
    with TestClient(app) as client:
        body = client.get("/stats/motors").json()
    assert body["by_motor"] == {}
    assert body["net_pnl_usd"] == 0
