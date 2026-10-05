"""Dinero ausente/corrupto no se convierte a cero ni a capacidad disponible."""

from decimal import Decimal

import pytest

from src.math.balance import available_cash_usd


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"balance": 20000}, "200"),
        ({"balance": 4998, "balance_dollars": "49.9825"}, "49.9825"),
        ({"balance_dollars": "0.0025"}, "0.0025"),
        ({"balance": 0, "balance_dollars": "0.0000"}, "0"),
    ],
)
def test_exact_cash(payload, expected):
    assert available_cash_usd(payload) == Decimal(expected)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"balance": None},
        {"balance": True},
        {"balance": -1},
        {"balance": 49.5},
        {"balance": "4998"},
        {"balance_dollars": None},
        {"balance_dollars": "NaN"},
        {"balance_dollars": "Infinity"},
        {"balance_dollars": "-1"},
        {"balance_dollars": "0.00001"},
        {"balance_dollars": 200},
        {"balance_dollars": "not-money"},
        {"balance_dollars": "1e1000"},
        {"balance_dollars": "2" * 65},
        {"balance": 20000, "balance_dollars": "INVALID_SECRET"},
        {"balance": 49, "balance_dollars": "49.9825"},
    ],
)
def test_bad_or_contradictory_response_fails_closed(payload):
    with pytest.raises(ValueError) as error:
        available_cash_usd(payload)
    assert "INVALID_SECRET" not in str(error.value)
