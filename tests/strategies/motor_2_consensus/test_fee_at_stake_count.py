"""
MOTOR_2_FEE_AT_STAKE_COUNT (auditoría 2026-07-12): la fee del edge medida al count REAL
del stake flat en vez de count=1.

La fee de Kalshi es ceil POR ORDEN: medirla con count=1 la sobreestima hasta +0.78pp por
contrato en asks bajos vs lo que la orden real (stake flat de 2-9 contratos) paga. El
funnel mostraba edges más pesimistas que la economía real de la orden.

Verifica: mecanismo (con count real el edge SUBE exactamente el sobrecosto del ceil),
control (flag off = comportamiento histórico EXACTO), y el redondeo del stake (mismo
int(stake·100 // ask) que usa el executor).
"""

from __future__ import annotations

from src.math.fees import kalshi_fee_cents
from src.strategies.motor_2_consensus.detector import _net_edge_pct, _stake_count


def test_default_count1_is_exact_historical_behavior():
    """CONTROL: sin count (default 1), el edge es idéntico a la fórmula histórica."""
    for ask in (20, 40, 50, 70, 90):
        fee1 = kalshi_fee_cents(1, ask)
        expected = (0.60 * 100.0 - ask - fee1) / 100.0
        assert _net_edge_pct(0.60, ask) == expected


def test_fee_at_real_count_raises_edge_by_ceil_overshoot():
    """MECANISMO: a ask=20 con 9 contratos (stake $1.80), la fee real por contrato es
    ceil(0.07·9·20·80/100)/9 = 11/9 ≈ 1.22¢ vs 2¢ a count=1 → el edge sube +0.78pp."""
    import pytest

    edge_c1 = _net_edge_pct(0.25, 20, count=1)
    edge_c9 = _net_edge_pct(0.25, 20, count=9)
    fee1 = kalshi_fee_cents(1, 20)  # 2¢
    fee9_pc = kalshi_fee_cents(9, 20) / 9  # ≈1.22¢
    assert edge_c9 - edge_c1 == pytest.approx((fee1 - fee9_pc) / 100.0)
    assert edge_c9 > edge_c1  # nunca puede BAJAR: ceil(n·f)/n ≤ ceil(f)


def test_stake_count_matches_executor_rounding():
    """El count del stake usa el MISMO redondeo que el executor (int(stake·100 // ask)):
    capital $180, stake 1% = $1.80 → 4 contratos a 40¢, 2 a 70¢, 1 a 99¢."""
    assert _stake_count(40, 180.0, 1.0) == 4
    assert _stake_count(70, 180.0, 1.0) == 2
    assert _stake_count(99, 180.0, 1.0) == 1


def test_stake_count_fail_safe_floor_one():
    """FAIL-SAFE: sin stake configurado (0) o inputs degenerados → count=1 (la fee queda
    definida y el edge cae al comportamiento histórico, nunca divide por cero)."""
    assert _stake_count(50, 180.0, 0.0) == 1  # sin flat → histórico
    assert _stake_count(50, 0.0, 1.0) == 1  # capital 0
    assert _stake_count(0, 180.0, 1.0) == 1  # ask fuera de rango
    assert _net_edge_pct(0.60, 50, count=0) == _net_edge_pct(0.60, 50, count=1)


def test_out_of_range_ask_still_no_edge():
    """CONTROL: ask fuera de 1..99 sigue devolviendo -1.0 con cualquier count."""
    assert _net_edge_pct(0.60, 0, count=5) == -1.0
    assert _net_edge_pct(0.60, 100, count=5) == -1.0
