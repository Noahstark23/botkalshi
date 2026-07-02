"""
Re-cálculo del shadow histórico con fees corregidas (radar post-fix 0dbf9b7).

Invariante central: las COTAS deben CONTENER la fee verdadera para cualquier
configuración de precios compatible con lo grabado (gross, count). Se verifica por
fuerza bruta contra kalshi_fee_cents sobre particiones reales.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.analytics.shadow_fee_recalc import (
    FEE_FIX_AT_DEFAULT,
    binary_fee_bounds,
    consensus_corrected_bounds,
    multi_fee_bounds,
    old_fee_cents,
    recompute_shadow,
)
from src.math.arbitrage import detect_binary_arb, detect_multi_outcome_arb
from src.math.fees import kalshi_fee_cents
from src.storage.models import EdgeWindow

PRE = FEE_FIX_AT_DEFAULT - timedelta(days=3)
POST = FEE_FIX_AT_DEFAULT + timedelta(hours=3)


# =====================================================
# Fórmula vieja pineada (fossil) — reconstruye lo que se descontó
# =====================================================


def test_old_fee_is_one_cent_per_contract_for_any_price():
    """La razón de que la corrección consensus sea exacta: fee vieja ≡ 1¢/contrato."""
    assert all(old_fee_cents(1, p) == 1 for p in range(1, 100))


def test_old_fee_understated_100x_at_scale():
    """El caso oficial de Kalshi: 100 contratos @50¢ = 175¢. La vieja daba 2¢."""
    assert old_fee_cents(100, 50) == 2
    assert kalshi_fee_cents(100, 50) == 175


# =====================================================
# consensus: corrección acotada exacta
# =====================================================


def test_consensus_bounds_bracket_true_correction():
    """Para todo ask: neto_corregido real ∈ [conservador, optimista]."""
    for ask in range(1, 100):
        fair_cents = ask + 6  # gross 6¢
        recorded = fair_cents - ask - old_fee_cents(1, ask)  # lo que se grabó (fee vieja)
        true_net = fair_cents - ask - kalshi_fee_cents(1, ask)
        conservative, optimistic = consensus_corrected_bounds(recorded)
        assert conservative <= true_net <= optimistic, f"ask={ask}"


# =====================================================
# binary: cotas exactas por escaneo de particiones
# =====================================================


@pytest.mark.parametrize("count", [1, 10, 100])
@pytest.mark.parametrize("s", [30, 60, 95, 99, 120, 150])
def test_binary_bounds_bracket_every_partition(count, s):
    lo, hi = binary_fee_bounds(count, s)
    for p1 in range(max(1, s - 99), min(99, s - 1) + 1):
        true_fee = kalshi_fee_cents(count, p1) + kalshi_fee_cents(count, s - p1)
        assert lo <= true_fee <= hi, f"count={count} s={s} p1={p1}"


def test_binary_bounds_tight_when_partition_unique():
    """s=2 solo admite (1,1) → cota degenerada exacta."""
    lo, hi = binary_fee_bounds(50, 2)
    assert lo == hi == 2 * kalshi_fee_cents(50, 1)


def test_binary_synthetic_opp_roundtrip():
    """Opp real (fee correcta) grabada con fee vieja → las cotas del re-cálculo
    contienen el neto verdadero, reconstruyendo s solo desde (gross, count)."""
    opp = detect_binary_arb("T", 40, 200, 45, 200)
    assert opp is not None
    sum_prices = 100 - opp.gross_profit_cents // opp.count
    lo, hi = binary_fee_bounds(opp.count, sum_prices)
    assert lo <= opp.fees_cents <= hi
    assert opp.gross_profit_cents - hi <= opp.net_profit_cents


# =====================================================
# multi_outcome: cotas por distribución extrema, n desconocido
# =====================================================


@pytest.mark.parametrize(
    "legs",
    [
        [("A", 30, 100), ("B", 30, 100), ("C", 30, 100)],
        [("A", 10, 50), ("B", 12, 50), ("C", 15, 50), ("D", 18, 50), ("E", 20, 50)],
        [("A", 40, 80), ("B", 45, 80)],
    ],
)
def test_multi_bounds_bracket_true_fee(legs):
    opp = detect_multi_outcome_arb("EV", legs)
    assert opp is not None
    sum_prices = 100 - opp.gross_profit_cents // opp.count
    lo, hi = multi_fee_bounds(opp.count, sum_prices)
    assert lo <= opp.fees_cents <= hi, f"fees={opp.fees_cents} bounds=({lo},{hi})"


def test_multi_bounds_invalid_sum_raises():
    with pytest.raises(ValueError):
        multi_fee_bounds(10, 1)  # ni 2 patas a 1¢ caben


# =====================================================
# recompute_shadow: agregación por población
# =====================================================


def _cw(magnitude: int, *, gross: int | None = None, fees: int | None = None) -> EdgeWindow:
    return EdgeWindow(
        market_ticker="T",
        magnitude_cents=magnitude,
        gross_spread_cents=gross,
        fees_cents=fees,
        kind="consensus",
        created_at=PRE if fees is None else POST,
    )


def _bw(
    magnitude: int,
    *,
    gross: int | None,
    count: int | None,
    created: datetime,
    kind: str | None = "binary",
) -> EdgeWindow:
    return EdgeWindow(
        market_ticker="T",
        magnitude_cents=magnitude,
        gross_spread_cents=gross,
        count=count,
        fees_cents=2,
        kind=kind,
        created_at=created,
    )


def test_recompute_consensus_populations():
    rows = [
        _cw(4),  # pre-fix: conservador 3 → NO supera umbral 3.0; optimista 4 sí
        _cw(8),  # pre-fix: sobrevive en ambos
        _cw(5, gross=7, fees=2),  # post-fix consistente → no se toca
        _cw(9, gross=4, fees=2),  # post-fix INCONSISTENTE (9 ≠ 4−2 ±1)
    ]
    r = recompute_shadow(rows, min_edge_pp=3.0)
    assert r.consensus.pre_fix == 2
    assert r.consensus.post_fix == 2
    assert r.consensus.pre_kept_optimistic == 2
    assert r.consensus.pre_kept_conservative == 1
    assert r.consensus.inconsistent_post == 1
    assert r.consensus.pre_mean_recorded_pp == pytest.approx(6.0)
    assert r.consensus.pre_mean_conservative_pp == pytest.approx(5.0)


def test_recompute_binary_phantom_detection():
    # Ventana pre-fix: 100 contratos, gross 300¢ (s=97), grabada con fee vieja (2¢) → net 298.
    # Fee correcta para s=97 ronda 130-340¢ según partición → con la cota conservadora
    # puede quedar ≤0 → fantasma; se verifica contra el cálculo real de la cota.
    gross, count = 300, 100
    lo, hi = binary_fee_bounds(count, 100 - gross // count)
    recorded_net = gross - old_fee_cents(count, 48) - old_fee_cents(count, 49)
    rows = [
        _bw(recorded_net, gross=gross, count=count, created=PRE),
        _bw(500, gross=520, count=10, created=POST),  # post-fix → no se toca
        _bw(50, gross=None, count=None, created=PRE),  # pre-2026-06 → irrecuperable
    ]
    r = recompute_shadow(rows)
    assert r.binary.pre_fix == 2
    assert r.binary.post_fix == 1
    assert r.binary.pre_uncorrectable == 1
    assert r.binary.pre_recorded_positive == 2
    expected_phantom = 1 if gross - hi <= 0 else 0
    assert r.binary.pre_phantom_conservative == expected_phantom
    assert r.binary.pre_still_positive_conservative == 1 - expected_phantom


def test_recompute_kind_null_treated_as_binary():
    rows = [_bw(50, gross=None, count=None, created=PRE, kind=None)]
    r = recompute_shadow(rows)
    assert r.binary.pre_fix == 1
    assert r.binary.pre_uncorrectable == 1


def test_recompute_multi_outcome_bucket():
    opp = detect_multi_outcome_arb("EV", [("A", 30, 100), ("B", 30, 100), ("C", 30, 100)])
    assert opp is not None
    old = sum(old_fee_cents(opp.count, 30) for _ in range(3))
    rows = [
        _bw(
            opp.gross_profit_cents - old,
            gross=opp.gross_profit_cents,
            count=opp.count,
            created=PRE,
            kind="multi_outcome",
        )
    ]
    r = recompute_shadow(rows)
    assert r.multi_outcome.pre_fix == 1
    assert r.multi_outcome.pre_uncorrectable == 0
    # el neto conservador reportado debe ser ≤ el neto verdadero (cota inferior)
    assert r.multi_outcome.pre_corrected_net_conservative_total_cents <= opp.net_profit_cents
