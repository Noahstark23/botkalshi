"""
Tests del Analyst Loop (loop engineering, advisory-only).

Cubre: el árbol `decide` en cada rama (puro), las skills de agregación sobre la DB, y
`analyze_once` (persiste AnalystVerdict, manda digest, compara con el veredicto previo).
DB temporal: conftest del paquete (autouse).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.analytics.analyst_loop import (
    AnalystLoop,
    ConsensusAgg,
    FunnelAgg,
    aggregate_consensus,
    aggregate_funnel,
    decide,
)

MIN = 3.0


def _funnel(**kw) -> FunnelAgg:
    base = {
        "cycles": 1,
        "matched": 0,
        "reject_absent": 0,
        "reject_cardinality": 0,
        "reject_names": 0,
        "reject_no_fair": 0,
        "best_pp": -1.0,
    }
    base.update(kw)
    return FunnelAgg(**base)


def _consensus(**kw) -> ConsensusAgg:
    base = {"total": 0, "gt_thresh": 0, "best_pp": -1.0, "mean_pp": 0.0}
    base.update(kw)
    return ConsensusAgg(**base)


# ── decide(): árbol determinístico ────────────────────────────────────────────


def test_decide_sin_datos():
    assert decide(_consensus(), _funnel(cycles=0), MIN)[0] == "sin_datos"


def test_decide_matching_bug_when_reject_names_dominant():
    f = _funnel(matched=0, reject_names=40, reject_absent=5)
    verdict, rec = decide(_consensus(), f, MIN)
    assert verdict == "matching_bug" and "alias" in rec.lower()


def test_decide_cardinalidad():
    f = _funnel(matched=0, reject_cardinality=30, reject_names=1)
    assert decide(_consensus(), f, MIN)[0] == "cardinalidad"


def test_decide_sin_prematch_when_absent_dominant():
    f = _funnel(matched=0, reject_absent=50, reject_names=0)
    assert decide(_consensus(), f, MIN)[0] == "sin_prematch"


def test_decide_mercado_eficiente_when_matched_but_low_edge():
    # matchea, pero el mejor edge visto (1.2pp) no cruza el umbral → eficiente.
    f = _funnel(matched=20, best_pp=1.2)
    verdict, rec = decide(_consensus(), f, MIN)
    assert verdict == "mercado_eficiente" and "MLB" in rec


def test_decide_edge_candidato_when_signals_fired():
    f = _funnel(matched=20, best_pp=4.5)
    c = _consensus(total=6, gt_thresh=6, best_pp=4.5)
    assert decide(c, f, MIN)[0] == "edge_candidato"


# ── skills de agregación ───────────────────────────────────────────────────────


def test_aggregate_consensus_and_funnel():
    now = datetime.now(UTC).replace(tzinfo=None)
    with models.get_session() as s:
        s.add(
            models.EdgeWindow(market_ticker="A", magnitude_cents=4, edge_pct=0.04, kind="consensus")
        )
        s.add(
            models.EdgeWindow(
                market_ticker="B", magnitude_cents=3, edge_pct=0.031, kind="consensus"
            )
        )
        s.add(
            models.EdgeWindow(
                market_ticker="C", magnitude_cents=5, edge_pct=0.05, kind="multi_outcome"
            )
        )
        s.add(models.Motor2FunnelSnapshot(events_matched=10, reject_names=2, best_net_edge_pp=1.5))
        s.add(models.Motor2FunnelSnapshot(events_matched=5, reject_absent=3, best_net_edge_pp=2.1))
        s.commit()
    since = now - timedelta(hours=24)
    with models.get_session() as s:
        c = aggregate_consensus(s, since, MIN)
        f = aggregate_funnel(s, since)
    assert c.total == 2  # solo consensus (multi_outcome excluido)
    assert c.gt_thresh == 2 and abs(c.best_pp - 4.0) < 1e-6
    assert f.cycles == 2 and f.matched == 15 and f.reject_names == 2
    assert abs(f.best_pp - 2.1) < 1e-6  # máximo entre ciclos


# ── analyze_once: persiste + reporta + compara con previo ────────────────────────


@pytest.mark.asyncio
async def test_analyze_once_persists_verdict_and_reports():
    # Estado "eficiente": embudo con matched>0 y best bajo, sin filas consensus.
    with models.get_session() as s:
        s.add(models.Motor2FunnelSnapshot(events_matched=12, best_net_edge_pp=1.1))
        s.commit()
    loop = AnalystLoop(interval_sec=3600, window_hours=24, min_edge_pp=MIN)
    with patch("src.analytics.analyst_loop.send_alert", new=AsyncMock()) as mock_alert:
        row = await loop.analyze_once()
    assert row.verdict == "mercado_eficiente"
    mock_alert.assert_awaited_once()  # mandó el digest
    with models.get_session() as s:
        saved = list(s.exec(select(models.AnalystVerdict)).all())
    assert len(saved) == 1 and saved[0].verdict == "mercado_eficiente"


@pytest.mark.asyncio
async def test_analyze_once_uses_previous_verdict_for_trend():
    # Primer veredicto previo a comparar.
    with models.get_session() as s:
        s.add(models.AnalystVerdict(verdict="edge_candidato", consensus_gt_thresh=8))
        s.add(models.Motor2FunnelSnapshot(events_matched=9, best_net_edge_pp=1.0))
        s.commit()
    loop = AnalystLoop(interval_sec=3600, window_hours=24, min_edge_pp=MIN)
    captured: list[str] = []

    async def _fake_alert(msg, **kw):
        captured.append(msg)
        return True

    with patch("src.analytics.analyst_loop.send_alert", new=_fake_alert):
        await loop.analyze_once()
    # El digest incluye la tendencia desde el gt_thresh previo (8 → 0).
    assert "tendencia" in captured[0] and "8→0" in captured[0]
