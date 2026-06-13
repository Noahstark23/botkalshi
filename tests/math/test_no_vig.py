"""
Tests de src/math/no_vig.py — remoción de vig con cuotas reales (2-way y 3-way).

Casos con cuotas de sportsbook reales para anclar la matemática contra números que
un operador puede verificar a mano.
"""

from __future__ import annotations

import pytest

from src.math.no_vig import (
    implied_prob,
    overround,
    remove_vig_additive,
    remove_vig_multiplicative,
)


def test_implied_prob_basic():
    assert implied_prob(2.0) == pytest.approx(0.5)
    assert implied_prob(4.0) == pytest.approx(0.25)


@pytest.mark.parametrize("bad", [1.0, 0.5, 0.0, -2.0])
def test_implied_prob_rejects_non_decimal_odds(bad):
    with pytest.raises(ValueError):
        implied_prob(bad)


def test_overround_two_way():
    # Moneyline -110/-110 ≈ cuota decimal 1.909 cada lado → vig ~4.7%.
    p = [implied_prob(1.909), implied_prob(1.909)]
    assert overround(p) == pytest.approx(0.0476, abs=1e-3)


def test_multiplicative_two_way_symmetric_gives_half():
    """Cuotas simétricas → 0.5/0.5 justas, suma exactamente 1."""
    p = [implied_prob(1.909), implied_prob(1.909)]
    fair = remove_vig_multiplicative(p)
    assert fair[0] == pytest.approx(0.5)
    assert fair[1] == pytest.approx(0.5)
    assert sum(fair) == pytest.approx(1.0)


def test_multiplicative_three_way_soccer_sums_to_one():
    """Fútbol 1X2 (Mundial): Local/Empate/Visitante → fair suma 1 y conserva el orden."""
    # Ej. Argentina 1.80 / Empate 3.60 / Rival 4.50.
    p = [implied_prob(1.80), implied_prob(3.60), implied_prob(4.50)]
    fair = remove_vig_multiplicative(p)
    assert sum(fair) == pytest.approx(1.0)
    # El favorito sigue siendo el más probable; el orden se conserva.
    assert fair[0] > fair[1] > fair[2]
    # El multiplicativo achica cada prob proporcionalmente (quita el vig).
    assert all(f < imp for f, imp in zip(fair, p, strict=True))


def test_additive_two_way_matches_formula():
    p = [implied_prob(1.80), implied_prob(2.10)]
    n = len(p)
    margin = (sum(p) - 1.0) / n
    fair = remove_vig_additive(p)
    assert fair[0] == pytest.approx(p[0] - margin)
    assert fair[1] == pytest.approx(p[1] - margin)
    assert sum(fair) == pytest.approx(1.0)


def test_additive_raises_on_longshot_underflow():
    """Aditivo con longshot + vig alto → prob ≤ 0 → ValueError (usar multiplicativo).

    3-way jugoso (~12% vig): Local 1.667 / Empate 2.0 / Longshot 50.0 → implícitas
    [0.60, 0.50, 0.02]. El margin/3 ≈ 0.040 > 0.02 → el longshot cae bajo 0.
    (En 2-way el aditivo nunca subdesborda: es imposible matemáticamente.)
    """
    p = [implied_prob(1.667), implied_prob(2.0), implied_prob(50.0)]
    with pytest.raises(ValueError, match="multiplicative"):
        remove_vig_additive(p)
    # El multiplicativo lo maneja sin problema.
    fair = remove_vig_multiplicative(p)
    assert all(f > 0 for f in fair)
    assert sum(fair) == pytest.approx(1.0)


@pytest.mark.parametrize("fn", [remove_vig_multiplicative, remove_vig_additive])
def test_rejects_fewer_than_two_outcomes(fn):
    with pytest.raises(ValueError):
        fn([0.5])


@pytest.mark.parametrize("fn", [remove_vig_multiplicative, remove_vig_additive])
def test_rejects_out_of_range_probs(fn):
    with pytest.raises(ValueError):
        fn([0.5, 1.5])  # >1 inválido
    with pytest.raises(ValueError):
        fn([0.5, 0.0])  # 0 inválido
