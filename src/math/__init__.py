"""API público de src.math — funciones matemáticas puras para los motores de trading."""

from __future__ import annotations

from src.math.arbitrage import ArbLeg, ArbOpportunity, detect_binary_arb, detect_multi_outcome_arb
from src.math.fees import kalshi_fee_cents
from src.math.kelly import quarter_kelly_size

__all__ = [
    "kalshi_fee_cents",
    "quarter_kelly_size",
    "ArbLeg",
    "ArbOpportunity",
    "detect_binary_arb",
    "detect_multi_outcome_arb",
]
