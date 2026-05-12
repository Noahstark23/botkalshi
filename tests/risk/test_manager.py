"""Tests para src/risk/manager.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.math.arbitrage import detect_binary_arb, detect_multi_outcome_arb
from src.risk.manager import RiskManager, TradeDecision


def _mock_settings(active_capital_usd: float, max_trade_size_pct: float) -> MagicMock:
    s = MagicMock()
    s.ACTIVE_CAPITAL_USD = active_capital_usd
    s.MAX_TRADE_SIZE_PCT = max_trade_size_pct
    return s


@pytest.fixture
def rm() -> RiskManager:
    return RiskManager()


# =====================================================
# Binary arb — within capital cap
# =====================================================


def test_check_pre_trade_approves_when_within_cap(rm: RiskManager) -> None:
    # cost_per_unit = 40+45 = 85¢; max = 5% of $300 = $15 = 1500¢
    # max_count_by_capital = 1500 // 85 = 17; allowed = min(10, 17) = 10
    opp = detect_binary_arb("T", 40, 200, 45, 200, max_count=10)
    assert opp is not None

    with patch("src.risk.manager.get_settings", return_value=_mock_settings(300.0, 5.0)):
        decision = rm.check_pre_trade(opp)

    assert decision.approved is True
    assert decision.max_allowed_count == 10
    assert decision.reason == "ok"


def test_check_pre_trade_caps_to_capital_limit(rm: RiskManager) -> None:
    # count=100 from opp, but capital allows only 17
    # max = 1500¢ // 85¢ = 17
    opp = detect_binary_arb("T", 40, 200, 45, 200, max_count=100)
    assert opp is not None

    with patch("src.risk.manager.get_settings", return_value=_mock_settings(300.0, 5.0)):
        decision = rm.check_pre_trade(opp)

    assert decision.approved is True
    assert decision.max_allowed_count == 17


def test_check_pre_trade_rejects_when_below_minimum(rm: RiskManager) -> None:
    # capital=$1, max 5% = $0.05 = 5¢; 5 // 85 = 0 → rejected
    opp = detect_binary_arb("T", 40, 200, 45, 200)
    assert opp is not None

    with patch("src.risk.manager.get_settings", return_value=_mock_settings(1.0, 5.0)):
        decision = rm.check_pre_trade(opp)

    assert decision.approved is False
    assert decision.max_allowed_count == 0
    assert "Capital cap" in decision.reason


# =====================================================
# Multi-outcome — cost computed correctly
# =====================================================


def test_check_pre_trade_multi_outcome_cost_correct(rm: RiskManager) -> None:
    # 4-way arb: prices [20, 25, 25, 20] = sum 90¢/unit, count=50
    # capital=$300, max 5% = $15 = 1500¢
    # max_count_by_capital = 1500 // 90 = 16; allowed = min(50, 16) = 16
    legs = [("A", 20, 100), ("B", 25, 100), ("C", 25, 100), ("D", 20, 100)]
    opp = detect_multi_outcome_arb("EVENT", legs, max_count=50)
    assert opp is not None

    with patch("src.risk.manager.get_settings", return_value=_mock_settings(300.0, 5.0)):
        decision = rm.check_pre_trade(opp)

    assert decision.approved is True
    assert decision.max_allowed_count == 16


# =====================================================
# TradeDecision is frozen dataclass
# =====================================================


def test_trade_decision_is_frozen() -> None:
    d = TradeDecision(approved=True, reason="ok", max_allowed_count=10)
    with pytest.raises((AttributeError, TypeError)):
        d.approved = False  # type: ignore[misc]
