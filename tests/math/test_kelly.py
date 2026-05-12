"""Tests para src/math/kelly.py."""

from __future__ import annotations

import pytest

from src.math.kelly import quarter_kelly_size

# =====================================================
# Casos documentados del spec
# =====================================================


def test_edge_clear_no_cap_hit():
    # full_kelly = (0.60 - 0.50) / 0.50 = 0.20
    # quarter = 0.05, max_pct fraction = 0.05 → fraction = 0.05
    # count = floor(0.05 * 30000 / 50) = 30
    assert quarter_kelly_size(0.60, 50, 30000) == 30


def test_high_edge_hits_cap():
    # full_kelly = (0.90 - 0.50) / 0.50 = 0.80
    # quarter = 0.20 → capped at 0.05
    # count = floor(0.05 * 30000 / 50) = 30
    assert quarter_kelly_size(0.90, 50, 30000) == 30


def test_small_edge_no_cap():
    # full_kelly = (0.52 - 0.50) / 0.50 = 0.04
    # quarter = 0.01 → not capped (< 0.05)
    # count = floor(0.01 * 30000 / 50) = 6
    assert quarter_kelly_size(0.52, 50, 30000) == 6


# =====================================================
# Sin edge o edge negativo → 0
# =====================================================


def test_no_edge_true_equals_implied():
    assert quarter_kelly_size(0.50, 50, 30000) == 0


def test_negative_edge_returns_zero():
    # true_prob < implied → full_kelly < 0
    assert quarter_kelly_size(0.40, 50, 30000) == 0


# =====================================================
# Bankroll edge cases
# =====================================================


def test_bankroll_zero_returns_zero():
    assert quarter_kelly_size(0.60, 50, 0) == 0


def test_bankroll_negative_raises():
    with pytest.raises(ValueError, match="bankroll_cents"):
        quarter_kelly_size(0.60, 50, -1)


# =====================================================
# Parámetros inválidos
# =====================================================


def test_true_prob_zero_raises():
    with pytest.raises(ValueError, match="true_prob"):
        quarter_kelly_size(0.0, 50, 30000)


def test_true_prob_one_raises():
    with pytest.raises(ValueError, match="true_prob"):
        quarter_kelly_size(1.0, 50, 30000)


def test_price_zero_raises():
    with pytest.raises(ValueError, match="price_cents"):
        quarter_kelly_size(0.60, 0, 30000)


def test_price_100_raises():
    with pytest.raises(ValueError, match="price_cents"):
        quarter_kelly_size(0.60, 100, 30000)


def test_kelly_fraction_zero_raises():
    with pytest.raises(ValueError, match="kelly_fraction"):
        quarter_kelly_size(0.60, 50, 30000, kelly_fraction=0.0)


def test_kelly_fraction_above_one_raises():
    with pytest.raises(ValueError, match="kelly_fraction"):
        quarter_kelly_size(0.60, 50, 30000, kelly_fraction=1.1)


def test_max_pct_zero_raises():
    with pytest.raises(ValueError, match="max_pct"):
        quarter_kelly_size(0.60, 50, 30000, max_pct=0.0)


def test_max_pct_above_100_raises():
    with pytest.raises(ValueError, match="max_pct"):
        quarter_kelly_size(0.60, 50, 30000, max_pct=100.1)


# =====================================================
# Variantes de kelly_fraction y max_pct
# =====================================================


def test_full_kelly_fraction_still_respects_cap():
    # kelly_fraction=1.0: full_kelly=0.80, no fraction cap → min(0.80, 0.05) = 0.05
    # count = floor(0.05 * 30000 / 50) = 30
    assert quarter_kelly_size(0.90, 50, 30000, kelly_fraction=1.0) == 30


def test_max_pct_10_increases_cap():
    # kelly_fraction=0.25, full_kelly=0.80 → quarter=0.20, max=0.10 → fraction=0.10
    # count = floor(0.10 * 30000 / 50) = 60
    assert quarter_kelly_size(0.90, 50, 30000, max_pct=10.0) == 60
