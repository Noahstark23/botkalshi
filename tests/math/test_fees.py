"""Tests para src/math/fees.py."""

from __future__ import annotations

import math
from fractions import Fraction

import pytest

from src.math.fees import kalshi_fee_cents

# =====================================================
# Caso documentado
# =====================================================


def test_documented_example():
    assert kalshi_fee_cents(100, 50) == 2


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
    # 7 * 1 * 1 * 99 / 1_000_000 = 693/1_000_000 → ceil = 1
    assert kalshi_fee_cents(1, 1) == 1


def test_price_99_positive_fee():
    # simétrico a price=1
    assert kalshi_fee_cents(1, 99) == 1


# =====================================================
# Casos boundary de float precision
# =====================================================
# Estos valores ejercitan los puntos donde float podría divergir:
# – N.0 exacto (no debería redondear para arriba)
# – N.999... (no debería redondear para abajo)


def test_boundary_exact_integer_price50_count400():
    # 7 * 400 * 50 * 50 = 7_000_000 → exactamente 7.0 → fee = 7
    assert kalshi_fee_cents(400, 50) == 7


def test_boundary_exact_integer_price25_count1600():
    # 7 * 1600 * 25 * 75 = 21_000_000 → exactamente 21.0 → fee = 21
    assert kalshi_fee_cents(1600, 25) == 21


def test_boundary_exact_integer_price75_count1600():
    # simétrico: 7 * 1600 * 75 * 25 = 21_000_000 → fee = 21
    assert kalshi_fee_cents(1600, 75) == 21


def test_boundary_near_one_price1_count1443():
    # 7 * 1443 * 1 * 99 = 999_999 → 0.999999 → ceil = 1
    assert kalshi_fee_cents(1443, 1) == 1


def test_boundary_near_one_price99_count1443():
    # simétrico al anterior
    assert kalshi_fee_cents(1443, 99) == 1


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
    # 7 * 1000 * 50 * 50 = 17_500_000 → 17.5 → ceil = 18
    assert kalshi_fee_cents(1000, 50) == 18


def test_large_count_10000():
    # 7 * 10000 * 50 * 50 = 175_000_000 → 175.0 → ceil = 175
    assert kalshi_fee_cents(10_000, 50) == 175


# =====================================================
# Equivalencia con fórmula float en rango completo
# (justifica el uso de int math)
# =====================================================


def test_integer_implementation_matches_float_formula():
    """La implementación entera coincide con la fórmula oficial para todos los inputs válidos.

    Usa Fraction como referencia de aritmética exacta en lugar de float, porque el float
    tiene bugs de precisión en boundary cases (e.g. count=10000, price=10:
    0.07*10000*10*90/10000 = 63.000000000000014 en float → ceil=64, pero el resultado
    matemático correcto es 63). Esto justifica el uso de int math en producción.
    """
    for count in [1, 10, 100, 1000, 10_000]:
        for price in range(1, 100):
            exact = math.ceil(Fraction(7 * count * price * (100 - price), 1_000_000))
            actual = kalshi_fee_cents(count, price)
            assert actual == exact, (
                f"Mismatch at count={count}, price={price}: int={actual}, exact={exact}"
            )
