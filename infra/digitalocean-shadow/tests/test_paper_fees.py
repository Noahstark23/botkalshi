"""Fees del simulador: schedule oficial (centicent) vs producción (ceil al centavo)."""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parents[1]))

import paper_fees as pf  # noqa: E402

from src.math.fees import kalshi_fee_cents  # noqa: E402


@pytest.mark.parametrize(
    ("count", "price", "expected_cc"),
    [
        (1, 50, 175),  # 0.07×0.25 = $0.0175 exacto
        (100, 50, 17_500),  # ejemplo oficial $1.75
        (1, 95, 34),  # 0.3325¢ → 0.0034 (ceil a centicent)
        (1, 1, 7),  # 0.0693¢ → 0.0007
        (3, 37, 490),  # 0.07×3×0.37×0.63 = 0.048951 → 0.0490
        (0, 50, 0),
        (10, 0, 0),
        (10, 100, 0),
    ],
)
def test_exact_matches_schedule_formula(count, price, expected_cc):
    assert pf.fee_centicents_exact(count, price) == expected_cc


def test_exact_maker_is_quarter_rate():
    assert pf.fee_centicents_exact(100, 50, fee_type="maker") == 4_375  # $0.4375
    assert pf.fee_centicents_exact(100, 50, fee_type="maker", fee_multiplier="0.5") == 2_188


def test_ceil_is_per_order_not_per_contract():
    """10 contratos a 50¢ en UNA orden ≠ 10 órdenes de 1: el ceil va sobre el total."""
    one_order = pf.fee_centicents_exact(10, 50)
    ten_orders = 10 * pf.fee_centicents_exact(1, 50)
    assert one_order == 1_750 and ten_orders == 1_750  # centicent: coinciden exacto
    # …pero al centavo (producción) medir a count=1 sobreestima ×1.14:
    assert kalshi_fee_cents(10, 50) == 18
    assert 10 * kalshi_fee_cents(1, 50) == 20


def test_conservative_delegates_to_production_and_never_underestimates():
    for count in (1, 2, 3, 7, 10, 25, 100):
        for price in range(1, 100):
            exact = pf.fee_centicents_exact(count, price)
            cons = pf.fee_cents_conservative(count, price) * 100
            assert cons == kalshi_fee_cents(count, price) * 100
            assert 0 <= cons - exact < 100  # cota superior, a menos de 1¢


def test_breakdown_reports_overestimate():
    b = pf.fee_breakdown(1, 50)
    assert b["exact_fee_usd"] == "0.0175"
    assert b["conservative_fee_usd"] == "0.0200"
    assert b["overestimate_usd"] == "0.0025"


@pytest.mark.parametrize("bad", [0.5, True])
def test_float_multiplier_rejected(bad):
    with pytest.raises(ValueError):
        pf.fee_centicents_exact(1, 50, fee_multiplier=bad)


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {"count": -1, "price_cents": 50},
        {"count": 1, "price_cents": 101},
        {"count": 1.0, "price_cents": 50},
        {"count": 1, "price_cents": 50, "fee_type": "mid"},
    ],
)
def test_invalid_inputs_raise(kwargs):
    with pytest.raises(ValueError):
        pf.fee_centicents_exact(**kwargs)


def test_parse_usd_to_centicents():
    assert pf.parse_usd_to_centicents("0.0175") == 175
    assert pf.parse_usd_to_centicents("1.75") == 17_500
    assert pf.parse_usd_to_centicents("0.00001") is None  # más fino que centicent
    assert pf.parse_usd_to_centicents(0.0175) is None  # float no
    assert pf.parse_usd_to_centicents("abc") is None
    assert pf.fee_centicents_exact(1, 50, fee_multiplier=Fraction(0)) == 0
