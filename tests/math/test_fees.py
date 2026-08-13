"""Tests para src/math/fees.py.

Los valores pineados vienen del fee schedule OFICIAL de Kalshi
(https://kalshi.com/docs/kalshi-fee-schedule.pdf y Help Center):
fee = round-up-al-centavo(0.07 × C × P × (1 − P)), con P en dólares.
En centavos enteros: fee_cents = ceil(7 × C × p × (100 − p) / 10_000).

Ejemplos publicados por Kalshi que estos tests pinnean:
  - 100 contratos @ 50¢ → $1.75 = 175¢ (el máximo por contrato: 1.75¢)
  - 1 contrato @ 95¢ → 0.3325¢ → redondea a 1¢

NOTA HISTÓRICA (auditoría 2026-07-01): la versión anterior usaba denominador
1_000_000 — eso produce el fee en DÓLARES ceileados, no en centavos, y estos
tests pineaban valores ~100× menores (p.ej. fee(100,50)==2). El fósil venía
de KALSHI_BOT_CONTEXT.md §6. No re-pinnear contra el código: pinnear contra
los ejemplos publicados por Kalshi.
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest

from src.math.fees import kalshi_fee_cents

# =====================================================
# Ejemplos OFICIALES del fee schedule de Kalshi
# =====================================================


def test_official_example_100_contracts_at_50c():
    # Fee schedule oficial: 100 contratos @ 50¢ → $1.75 = 175¢
    assert kalshi_fee_cents(100, 50) == 175


def test_official_example_1_contract_at_50c():
    # 0.07 * 0.50 * 0.50 = $0.0175 → round up al centavo → 2¢
    assert kalshi_fee_cents(1, 50) == 2


def test_official_example_1_contract_at_95c():
    # 0.07 * 0.95 * 0.05 = $0.003325 → round up al centavo → 1¢
    assert kalshi_fee_cents(1, 95) == 1


def test_fee_symmetry_p_vs_100_minus_p():
    # p·(100−p) es simétrico: fee(c, p) == fee(c, 100−p)
    for price in (1, 10, 30, 50):
        assert kalshi_fee_cents(37, price) == kalshi_fee_cents(37, 100 - price)


# =====================================================
# Casos especiales (retornan 0 sin raise)
# =====================================================


def test_count_zero_returns_zero():
    assert kalshi_fee_cents(0, 50) == 0


def test_price_zero_returns_zero():
    assert kalshi_fee_cents(100, 0) == 0


def test_price_100_returns_zero():
    assert kalshi_fee_cents(100, 100) == 0


# =====================================================
# Boundary prices
# =====================================================


def test_price_1_positive_fee():
    # 7 * 1 * 1 * 99 = 693 → 693/10_000 = 0.0693 → ceil = 1
    assert kalshi_fee_cents(1, 1) == 1


def test_price_99_positive_fee():
    # simétrico a price=1
    assert kalshi_fee_cents(1, 99) == 1


# =====================================================
# Casos boundary de la aritmética entera
# =====================================================
# – N.0 exacto (no debe redondear para arriba)
# – N.9999 (no debe redondear para abajo)


def test_boundary_exact_integer_price50_count400():
    # 7 * 400 * 50 * 50 = 7_000_000 → exactamente 700.0 → fee = 700
    assert kalshi_fee_cents(400, 50) == 700


def test_boundary_exact_integer_price25_count1600():
    # 7 * 1600 * 25 * 75 = 21_000_000 → exactamente 2100.0 → fee = 2100
    assert kalshi_fee_cents(1600, 25) == 2100


def test_boundary_exact_integer_price75_count1600():
    # simétrico: 7 * 1600 * 75 * 25 = 21_000_000 → fee = 2100
    assert kalshi_fee_cents(1600, 75) == 2100


def test_boundary_near_integer_price1_count1443():
    # 7 * 1443 * 1 * 99 = 999_999 → 99.9999 → ceil = 100
    assert kalshi_fee_cents(1443, 1) == 100


def test_boundary_near_integer_price99_count1443():
    # simétrico al anterior
    assert kalshi_fee_cents(1443, 99) == 100


# =====================================================
# Inputs inválidos
# =====================================================


def test_negative_count_raises():
    with pytest.raises(ValueError, match="count"):
        kalshi_fee_cents(-1, 50)


def test_negative_price_raises():
    with pytest.raises(ValueError, match="price_cents"):
        kalshi_fee_cents(1, -1)


def test_price_above_100_raises():
    with pytest.raises(ValueError, match="price_cents"):
        kalshi_fee_cents(1, 101)


# =====================================================
# Counts grandes (no overflow, resultado correcto)
# =====================================================


def test_large_count_1000():
    # 7 * 1000 * 50 * 50 = 17_500_000 → 1750.0 → fee = 1750 ($17.50)
    assert kalshi_fee_cents(1000, 50) == 1750


def test_large_count_10000():
    # 7 * 10000 * 50 * 50 = 175_000_000 → 17_500 ($175)
    assert kalshi_fee_cents(10_000, 50) == 17_500


# =====================================================
# Equivalencia con la fórmula oficial en rango completo
# (justifica el uso de int math)
# =====================================================


def test_integer_implementation_matches_official_formula():
    """La implementación entera coincide con la fórmula oficial para todos los inputs válidos.

    Usa Fraction como referencia de aritmética exacta en lugar de float (float tiene bugs
    de precisión en boundaries, p.ej. 0.07*C*P*(1-P) puede dar N.000000000000014 → ceil
    incorrecto). La referencia es fee_cents = ceil(7·C·p·(100−p) / 10_000), equivalente
    exacto en centavos de round-up-al-centavo(0.07·C·(p/100)·((100−p)/100) dólares).
    """
    for count in [1, 10, 100, 1000, 10_000]:
        for price in range(1, 100):
            exact = math.ceil(Fraction(7 * count * price * (100 - price), 10_000))
            actual = kalshi_fee_cents(count, price)
            assert actual == exact, (
                f"Mismatch at count={count}, price={price}: int={actual}, exact={exact}"
            )


# =====================================================
# Fee de MAKER (verificada 2026-08-13 contra el PDF oficial July 7, 2026)
# =====================================================
# maker = round up(M × 0.0175 × C × P × (1−P)) — ¼ del taker, multiplicador 1 en
# deportes ("KXMLBGAME | 1 | 1"). El $0 existe solo en series no deportivas.


def test_maker_fee_ejemplo_oficial_cuarto_del_taker():
    from src.math.fees import kalshi_maker_fee_cents

    # Taker oficial: fee(100, 50) = 175¢. Maker exacto: 0.0175×100×0.25 = 43.75¢ → 44.
    assert kalshi_maker_fee_cents(100, 50) == 44


def test_maker_fee_ceil_por_orden():
    from src.math.fees import kalshi_maker_fee_cents

    assert kalshi_maker_fee_cents(10, 50) == 5  # 4.375¢ → 5 (ceil)
    assert kalshi_maker_fee_cents(1, 50) == 1  # 0.4375¢ → 1 (medir a count=1 sobreestima)


def test_maker_fee_bordes():
    from src.math.fees import kalshi_maker_fee_cents

    assert kalshi_maker_fee_cents(0, 50) == 0
    assert kalshi_maker_fee_cents(100, 0) == 0
    assert kalshi_maker_fee_cents(100, 100) == 0
