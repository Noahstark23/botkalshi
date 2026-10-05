from decimal import Decimal
from fractions import Fraction
from math import ceil

import pytest

from src.math.fees import kalshi_fee_cents, kalshi_maker_fee_cents
from src.math.quantities import contract_count, fixed_count, precise_usd


@pytest.mark.parametrize("raw", ["1.23", Decimal("1.23"), Fraction(123, 100)])
def test_quantity_round_trip_is_exact(raw):
    assert contract_count(raw) == Fraction(123, 100)
    assert fixed_count(contract_count(raw)) == "1.23"


@pytest.mark.parametrize(
    "raw",
    [True, None, "NaN", "Infinity", "-0.01", "0.001", "1e-1000000", "1e1000000", Fraction(1, 3)],
)
def test_unrepresentable_quantities_never_round_to_zero(raw):
    assert contract_count(raw) is None


@pytest.mark.parametrize("count", [Fraction(1, 100), Fraction(123, 100), Fraction(5, 2), 100])
@pytest.mark.parametrize("price", [1, 47, 50, 99])
@pytest.mark.parametrize("multiplier", [Fraction(1), Fraction(1, 2), Fraction(1, 100000)])
def test_fractional_fee_ceiling_matches_exact_rational_formula(count, price, multiplier):
    base = Fraction(count) * price * (100 - price) * multiplier
    assert kalshi_fee_cents(count, price, fee_multiplier=multiplier) == ceil(
        base * Fraction(7, 10000)
    )
    assert kalshi_maker_fee_cents(count, price, fee_multiplier=multiplier) == ceil(
        base * Fraction(7, 40000)
    )


def test_signed_amount_preserves_subcent_pnl():
    assert precise_usd(Fraction(6519, 100)) == "0.6519"
    assert precise_usd(Fraction(-5781, 100)) == "-0.5781"
