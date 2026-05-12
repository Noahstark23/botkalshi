"""Tests para src/math/arbitrage.py."""

from __future__ import annotations

import pytest

from src.math.arbitrage import detect_binary_arb, detect_multi_outcome_arb
from src.math.fees import kalshi_fee_cents

# =====================================================
# detect_binary_arb — happy path
# =====================================================


def test_binary_clear_opportunity_values():
    # yes=40, no=45: sum=85, gross=(100-85)*100=1500
    # fee_yes=kalshi_fee_cents(100,40)=17, fee_no=kalshi_fee_cents(100,45)=18
    # net=1465, total_cost=8500, edge≈17.24%
    result = detect_binary_arb("TICKER", 40, 200, 45, 200)
    assert result is not None
    assert result.count == 100  # max_count default
    assert result.gross_profit_cents == 1500
    assert result.fees_cents == kalshi_fee_cents(100, 40) + kalshi_fee_cents(100, 45)
    assert result.net_profit_cents == 1500 - result.fees_cents
    assert result.total_cost_cents == (40 + 45) * 100


def test_binary_opportunity_legs_structure():
    result = detect_binary_arb("TICKER", 40, 200, 45, 200)
    assert result is not None
    assert len(result.legs) == 2
    yes_leg = next(leg for leg in result.legs if leg.side == "yes")
    no_leg = next(leg for leg in result.legs if leg.side == "no")
    assert yes_leg.price_cents == 40
    assert no_leg.price_cents == 45
    assert yes_leg.count == no_leg.count == 100


# =====================================================
# detect_binary_arb — no arb conditions
# =====================================================


def test_binary_no_arb_sum_above_100():
    assert detect_binary_arb("T", 50, 100, 55, 100) is None


def test_binary_no_arb_sum_exactly_100():
    assert detect_binary_arb("T", 50, 100, 50, 100) is None


def test_binary_fees_eat_gross_profit():
    # yes=49, no=49: sum=98, gross=2/contract
    # count=1: fee_yes=1, fee_no=1, net=2-2=0 → None
    result = detect_binary_arb("T", 49, 100, 49, 100, max_count=1)
    assert result is None


def test_binary_yes_size_zero_returns_none():
    assert detect_binary_arb("T", 40, 0, 45, 50) is None


def test_binary_no_size_zero_returns_none():
    assert detect_binary_arb("T", 40, 50, 45, 0) is None


# =====================================================
# detect_binary_arb — liquidity cap
# =====================================================


def test_binary_limited_by_yes_size():
    # yes_size=5 is the bottleneck
    result = detect_binary_arb("T", 40, 5, 45, 50)
    assert result is not None
    assert result.count == 5


def test_binary_limited_by_no_size():
    result = detect_binary_arb("T", 40, 50, 45, 3)
    assert result is not None
    assert result.count == 3


def test_binary_limited_by_max_count():
    result = detect_binary_arb("T", 40, 200, 45, 200, max_count=10)
    assert result is not None
    assert result.count == 10


# =====================================================
# detect_binary_arb — min_edge_pct filter
# =====================================================


def test_binary_min_edge_pct_filters_real_opportunity():
    # Edge is ~17.24%, filtering at 50% should give None
    assert detect_binary_arb("T", 40, 200, 45, 200, min_edge_pct=50.0) is None


def test_binary_min_edge_pct_zero_allows_opportunity():
    result = detect_binary_arb("T", 40, 200, 45, 200, min_edge_pct=0.0)
    assert result is not None


def test_binary_min_edge_pct_at_actual_edge_allows():
    result = detect_binary_arb("T", 40, 200, 45, 200)
    assert result is not None
    # Exactly at actual edge should still pass (edge_pct >= min_edge_pct)
    result2 = detect_binary_arb("T", 40, 200, 45, 200, min_edge_pct=result.edge_pct)
    assert result2 is not None


# =====================================================
# detect_binary_arb — invalid inputs
# =====================================================


def test_binary_yes_ask_zero_raises():
    with pytest.raises(ValueError, match="yes_ask_cents"):
        detect_binary_arb("T", 0, 100, 45, 100)


def test_binary_yes_ask_100_raises():
    with pytest.raises(ValueError, match="yes_ask_cents"):
        detect_binary_arb("T", 100, 100, 45, 100)


def test_binary_no_ask_zero_raises():
    with pytest.raises(ValueError, match="no_ask_cents"):
        detect_binary_arb("T", 40, 100, 0, 100)


def test_binary_no_ask_100_raises():
    with pytest.raises(ValueError, match="no_ask_cents"):
        detect_binary_arb("T", 40, 100, 100, 100)


def test_binary_yes_size_negative_raises():
    with pytest.raises(ValueError, match="yes_available_size"):
        detect_binary_arb("T", 40, -1, 45, 100)


def test_binary_no_size_negative_raises():
    with pytest.raises(ValueError, match="no_available_size"):
        detect_binary_arb("T", 40, 100, 45, -1)


def test_binary_max_count_zero_raises():
    with pytest.raises(ValueError, match="max_count"):
        detect_binary_arb("T", 40, 100, 45, 100, max_count=0)


def test_binary_min_edge_negative_raises():
    with pytest.raises(ValueError, match="min_edge_pct"):
        detect_binary_arb("T", 40, 100, 45, 100, min_edge_pct=-1.0)


# =====================================================
# ArbOpportunity.total_cost_cents property
# =====================================================


def test_total_cost_cents_property():
    result = detect_binary_arb("T", 40, 200, 45, 200)
    assert result is not None
    # (40 + 45) * 100 = 8500
    assert result.total_cost_cents == 8500


# =====================================================
# detect_multi_outcome_arb — happy path
# =====================================================


def test_multi_three_way_opportunity_values():
    # [30, 30, 30]: sum=90, gross=(100-90)*100=1000
    # fee per leg = kalshi_fee_cents(100, 30) = 15, total=45
    # net = 955
    legs = [("A", 30, 100), ("B", 30, 100), ("C", 30, 100)]
    result = detect_multi_outcome_arb("EVENT", legs)
    assert result is not None
    assert result.count == 100
    assert result.gross_profit_cents == 1000
    assert result.fees_cents == kalshi_fee_cents(100, 30) * 3
    assert result.net_profit_cents == 1000 - result.fees_cents
    assert len(result.legs) == 3
    assert all(leg.side == "yes" for leg in result.legs)


def test_multi_three_way_liquidity_defines_count():
    # min size = 5 (market A)
    legs = [("A", 30, 5), ("B", 30, 100), ("C", 30, 50)]
    result = detect_multi_outcome_arb("EVENT", legs)
    assert result is not None
    assert result.count == 5


# =====================================================
# detect_multi_outcome_arb — no arb conditions
# =====================================================


def test_multi_sum_above_100_returns_none():
    legs = [("A", 35, 100), ("B", 35, 100), ("C", 35, 100)]  # sum=105
    assert detect_multi_outcome_arb("EVENT", legs) is None


def test_multi_four_way_expensive_returns_none():
    # sum = 40+30+20+15 = 105 >= 100
    legs = [("A", 40, 100), ("B", 30, 100), ("C", 20, 100), ("D", 15, 100)]
    assert detect_multi_outcome_arb("EVENT", legs) is None


def test_multi_all_sizes_zero_returns_none():
    legs = [("A", 30, 0), ("B", 30, 0), ("C", 30, 0)]
    assert detect_multi_outcome_arb("EVENT", legs) is None


# =====================================================
# detect_multi_outcome_arb — invalid inputs
# =====================================================


def test_multi_single_leg_raises():
    with pytest.raises(ValueError, match="2 legs"):
        detect_multi_outcome_arb("EVENT", [("A", 40, 100)])


def test_multi_empty_legs_raises():
    with pytest.raises(ValueError, match="2 legs"):
        detect_multi_outcome_arb("EVENT", [])


def test_multi_ask_zero_raises():
    with pytest.raises(ValueError, match="ask_cents"):
        detect_multi_outcome_arb("EVENT", [("A", 0, 100), ("B", 30, 100)])


def test_multi_ask_100_raises():
    with pytest.raises(ValueError, match="ask_cents"):
        detect_multi_outcome_arb("EVENT", [("A", 100, 100), ("B", 30, 100)])


def test_multi_size_negative_raises():
    with pytest.raises(ValueError, match="available_size"):
        detect_multi_outcome_arb("EVENT", [("A", 30, -1), ("B", 30, 100)])


def test_multi_max_count_zero_raises():
    legs = [("A", 30, 100), ("B", 30, 100)]
    with pytest.raises(ValueError, match="max_count"):
        detect_multi_outcome_arb("EVENT", legs, max_count=0)


def test_multi_min_edge_negative_raises():
    legs = [("A", 30, 100), ("B", 30, 100)]
    with pytest.raises(ValueError, match="min_edge_pct"):
        detect_multi_outcome_arb("EVENT", legs, min_edge_pct=-1.0)


def test_multi_fees_eat_profit_returns_none():
    # yes=49, no=49: sum=98, gross=2/contract; at count=1 fees=2, net=0 → None
    assert detect_multi_outcome_arb("EVENT", [("A", 49, 1), ("B", 49, 1)]) is None


def test_multi_min_edge_pct_filters_opportunity():
    # [30,30,30] sum=90: edge≈10.6%; filter at 50% → None
    legs = [("A", 30, 100), ("B", 30, 100), ("C", 30, 100)]
    assert detect_multi_outcome_arb("EVENT", legs, min_edge_pct=50.0) is None


# =====================================================
# ArbOpportunity is immutable (frozen dataclass)
# =====================================================


def test_arb_opportunity_is_frozen():
    result = detect_binary_arb("T", 40, 200, 45, 200)
    assert result is not None
    with pytest.raises((AttributeError, TypeError)):
        result.count = 999  # type: ignore[misc]
