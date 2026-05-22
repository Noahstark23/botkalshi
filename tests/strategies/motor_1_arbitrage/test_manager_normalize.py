"""
Unit tests for manager_normalize — pure payload normalization layer.

All tests use raw message shapes verified against production Kalshi WS payloads
from 2026-05-22. Critical invariant: prices must be Decimal-converted
(float("0.97") * 100 = 96.999... → wrong cent; Decimal("0.97") * 100 = 97 → correct).
"""
from __future__ import annotations

import pytest

from src.strategies.motor_1_arbitrage.manager_normalize import (
    extract_sid,
    extract_ticker,
    normalize_delta_payload,
    normalize_snapshot_payload,
)


# =====================================================
# _price_to_cents — tested implicitly through normalizers
# =====================================================


@pytest.mark.parametrize(
    "price_str, expected_cents",
    [
        ("0.97", 97),
        ("0.9700", 97),  # trailing zeros
        ("0.03", 3),
        ("0.50", 50),
        ("0.01", 1),
        ("0.99", 99),
    ],
)
def test_snapshot_price_to_cents_exact(price_str, expected_cents):
    """Decimal conversion is exact — no float drift (float('0.97')*100 = 96.999...)."""
    raw = {
        "seq": 1,
        "msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [[price_str, "100"]],
            "no_dollars_fp": [],
        },
    }
    result = normalize_snapshot_payload(raw)
    assert result["yes"][0][0] == expected_cents


@pytest.mark.parametrize(
    "price_str, expected_cents",
    [
        ("0.97", 97),
        ("0.03", 3),
        ("0.50", 50),
    ],
)
def test_delta_price_to_cents_exact(price_str, expected_cents):
    raw = {
        "seq": 5,
        "msg": {
            "market_ticker": "T",
            "side": "yes",
            "price_dollars": price_str,
            "delta_fp": "10",
        },
    }
    result = normalize_delta_payload(raw, synthetic_previous_seq=0)
    assert result["price"] == expected_cents


# =====================================================
# _size_to_int — rounding
# =====================================================


@pytest.mark.parametrize(
    "size_raw, expected",
    [
        ("502000.00", 502000),
        ("3.16", 3),
        ("3.5", 4),  # round half up
        ("-62.00", -62),
        (100, 100),
        (7.9, 8),
    ],
)
def test_snapshot_size_to_int(size_raw, expected):
    raw = {
        "seq": 1,
        "msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.50", size_raw]],
            "no_dollars_fp": [],
        },
    }
    result = normalize_snapshot_payload(raw)
    assert result["yes"][0][1] == expected


# =====================================================
# normalize_snapshot_payload — happy path
# =====================================================


def test_normalize_snapshot_full():
    """Production-shape snapshot — yes + no with multiple levels."""
    raw = {
        "seq": 42,
        "msg": {
            "market_ticker": "INXD-26MAY21-B5210",
            "yes_dollars_fp": [["0.97", "1000"], ["0.96", "500"]],
            "no_dollars_fp": [["0.03", "1000"], ["0.04", "500"]],
        },
    }
    result = normalize_snapshot_payload(raw)

    assert result["seq"] == 42
    assert result["yes"] == [[97, 1000], [96, 500]]
    assert result["no"] == [[3, 1000], [4, 500]]


def test_normalize_snapshot_empty_sides():
    """Sides with no levels should produce empty lists, not raise."""
    raw = {
        "seq": 1,
        "msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [],
            "no_dollars_fp": [],
        },
    }
    result = normalize_snapshot_payload(raw)
    assert result["yes"] == []
    assert result["no"] == []


def test_normalize_snapshot_missing_sides_defaults_to_empty():
    """If yes_dollars_fp / no_dollars_fp keys are absent, treat as empty."""
    raw = {
        "seq": 1,
        "msg": {"market_ticker": "T"},
    }
    result = normalize_snapshot_payload(raw)
    assert result["yes"] == []
    assert result["no"] == []


def test_normalize_snapshot_missing_seq_raises():
    raw = {"msg": {"market_ticker": "T", "yes_dollars_fp": [], "no_dollars_fp": []}}
    with pytest.raises(ValueError, match="snapshot normalize failed"):
        normalize_snapshot_payload(raw)


def test_normalize_snapshot_missing_msg_raises():
    raw = {"seq": 1}
    with pytest.raises(ValueError, match="snapshot normalize failed"):
        normalize_snapshot_payload(raw)


def test_normalize_snapshot_bad_price_raises():
    raw = {
        "seq": 1,
        "msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["NOT_A_NUMBER", "100"]],
            "no_dollars_fp": [],
        },
    }
    with pytest.raises(ValueError):
        normalize_snapshot_payload(raw)


# =====================================================
# normalize_delta_payload — happy path
# =====================================================


def test_normalize_delta_yes_side():
    raw = {
        "seq": 10,
        "msg": {
            "market_ticker": "INXD-26MAY21-B5210",
            "side": "yes",
            "price_dollars": "0.97",
            "delta_fp": "-500.00",
        },
    }
    result = normalize_delta_payload(raw, synthetic_previous_seq=3)

    assert result["seq"] == 10
    assert result["previous_seq"] == 3
    assert result["side"] == "yes"
    assert result["price"] == 97
    assert result["delta"] == -500


def test_normalize_delta_no_side():
    raw = {
        "seq": 11,
        "msg": {
            "market_ticker": "T",
            "side": "NO",  # uppercase — must be lowercased
            "price_dollars": "0.03",
            "delta_fp": "200",
        },
    }
    result = normalize_delta_payload(raw, synthetic_previous_seq=7)
    assert result["side"] == "no"
    assert result["price"] == 3
    assert result["delta"] == 200


def test_normalize_delta_invalid_side_raises():
    raw = {
        "seq": 5,
        "msg": {
            "market_ticker": "T",
            "side": "maybe",
            "price_dollars": "0.50",
            "delta_fp": "10",
        },
    }
    with pytest.raises(ValueError, match="invalid side"):
        normalize_delta_payload(raw, synthetic_previous_seq=0)


def test_normalize_delta_missing_price_raises():
    raw = {
        "seq": 5,
        "msg": {"market_ticker": "T", "side": "yes", "delta_fp": "10"},
    }
    with pytest.raises(ValueError, match="delta normalize failed"):
        normalize_delta_payload(raw, synthetic_previous_seq=0)


def test_normalize_delta_missing_delta_raises():
    raw = {
        "seq": 5,
        "msg": {"market_ticker": "T", "side": "yes", "price_dollars": "0.50"},
    }
    with pytest.raises(ValueError, match="delta normalize failed"):
        normalize_delta_payload(raw, synthetic_previous_seq=0)


def test_normalize_delta_previous_seq_passthrough():
    """synthetic_previous_seq flows through unchanged — caller controls it."""
    raw = {
        "seq": 99,
        "msg": {
            "market_ticker": "T",
            "side": "yes",
            "price_dollars": "0.50",
            "delta_fp": "1",
        },
    }
    result = normalize_delta_payload(raw, synthetic_previous_seq=42)
    assert result["previous_seq"] == 42


# =====================================================
# extract_ticker
# =====================================================


def test_extract_ticker_happy_path():
    raw = {"seq": 1, "msg": {"market_ticker": "INXD-26MAY21-B5210"}}
    assert extract_ticker(raw) == "INXD-26MAY21-B5210"


def test_extract_ticker_missing_msg_returns_none():
    assert extract_ticker({"seq": 1}) is None


def test_extract_ticker_missing_market_ticker_returns_none():
    assert extract_ticker({"seq": 1, "msg": {}}) is None


def test_extract_ticker_msg_not_dict_returns_none():
    assert extract_ticker({"seq": 1, "msg": None}) is None


# =====================================================
# extract_sid
# =====================================================


def test_extract_sid_present():
    assert extract_sid({"sid": 7, "seq": 1}) == 7


def test_extract_sid_missing_returns_none():
    assert extract_sid({"seq": 1}) is None


def test_extract_sid_non_int_returns_none():
    assert extract_sid({"sid": "abc", "seq": 1}) is None


def test_extract_sid_zero_is_valid():
    assert extract_sid({"sid": 0}) == 0
