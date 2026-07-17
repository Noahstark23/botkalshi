"""
Techo de edge de EJECUCIÓN de Motor 2 (anti-fantasma) — MOTOR_2_MAX_EDGE_PCT.

Evidencia (auditoría de trades reales, settled): bucket edge ~5% → +$189 (59% win);
buckets 12-13% → −$621 (≤54% win); el día −$535 fue 13/13 trades con edge 8.9-13%.
Un "edge consensus" alto sobre un binario líquido MLB es casi siempre artefacto
(consenso mal calibrado), no valor.

El techo es un filtro ADITIVO de ejecución (estricto `>`): NO toca MIN_EDGE, sizing,
matching, ni el backstop MAX_PLAUSIBLE_EDGE=0.15 (que sigue arriba como red
anti-artefacto-monstruoso). Rechazos → diag["reject_high_edge"] (funnel: rej_high).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import (
    KalshiEventQuotes,
    KalshiQuote,
    find_signals,
)
from src.strategies.motor_2_consensus.matcher import ET, start_time_et

_KEY_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
NOW = datetime(2026, 6, 27, 12, 0, tzinfo=ET).astimezone(UTC)


def _stamp(dt: datetime) -> str:
    et = start_time_et(dt)
    return f"{et.year % 100:02d}{_KEY_MONTHS[et.month - 1]}{et.day:02d}"


def _fixture(phi_yes_ask: int) -> tuple[KalshiEventQuotes, OddsEvent]:
    """fair PHI ≈ 0.619 (1.60/2.60 sin vig) → edge del yes@PHI controlado por el ask:
    ask 55 ≈ 4.9pp · ask 48 ≈ 11.9pp · ask 40 ≈ 19.9pp. Las demás patas: edge negativo."""
    commence = NOW + timedelta(hours=6)
    odds = OddsEvent(
        id="x",
        sport_key="baseball_mlb",
        commence_time=commence,
        home_team="Philadelphia Phillies",
        away_team="New York Mets",
        bookmakers=(
            Bookmaker(
                key="pinnacle",
                title="P",
                markets=(
                    Market(
                        key="h2h",
                        outcomes=(
                            Outcome(name="Philadelphia Phillies", price=1.60),
                            Outcome(name="New York Mets", price=2.60),
                        ),
                    ),
                ),
            ),
        ),
    )
    key = f"KXMLBGAME-{_stamp(commence)}PHINYM"
    ke = KalshiEventQuotes(
        event_key=key,
        outcomes=(
            KalshiQuote(f"{key}-PHI", "Philadelphia Phillies", phi_yes_ask, 65),
            KalshiQuote(f"{key}-NYM", "New York Mets", 45, 65),
        ),
    )
    return ke, odds


def _find(phi_yes_ask: int, max_edge: float | None, diag: dict | None = None):
    ke, odds = _fixture(phi_yes_ask)
    return find_signals([ke], [odds], min_edge=0.03, now=NOW, diag=diag, max_edge=max_edge)


def test_max_edge_rejects_high_signal():
    """edge ~11.9pp con techo 8pp → 0 señales, contado como reject_high_edge."""
    diag: dict[str, float] = {}
    assert _find(48, 0.08, diag) == []
    assert diag["reject_high_edge"] == 1.0


def test_max_edge_none_preserves_behavior():
    """Sin techo (None) el comportamiento actual queda intacto: la señal de 11.9pp sale."""
    sigs = _find(48, None)
    assert len(sigs) == 1
    assert sigs[0].edge_pct > 0.08


def test_max_edge_boundary_exact_edge_is_emitted():
    """El techo es estricto (`>`): edge == max_edge exacto → se emite."""
    baseline = _find(48, None)
    assert len(baseline) == 1
    edge = baseline[0].edge_pct
    sigs = _find(48, edge)  # techo EXACTAMENTE en el edge de la señal
    assert len(sigs) == 1


def test_ceiling_fires_before_backstop():
    """edge ~19.9pp (> backstop 15%) con techo 8pp → lo atrapa el TECHO (rej_high), y el
    backstop sigue intacto por encima (sin techo, ese edge lo descarta el backstop)."""
    diag: dict[str, float] = {}
    assert _find(40, 0.08, diag) == []
    assert diag["reject_high_edge"] == 1.0
    # Control: sin techo, el mismo edge lo mata el backstop (no se emite, y NO es rej_high).
    diag2: dict[str, float] = {}
    assert _find(40, None, diag2) == []
    assert diag2.get("reject_high_edge", 0.0) == 0.0


def test_legit_bucket_still_flows_under_ceiling():
    """Regresión: el bucket rentable (~5pp) sigue emitiendo con el techo default 8pp."""
    diag: dict[str, float] = {}
    sigs = _find(55, 0.08, diag)
    assert len(sigs) == 1
    assert 0.03 < sigs[0].edge_pct <= 0.08
    assert diag["reject_high_edge"] == 0.0
