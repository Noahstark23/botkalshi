"""Pre-trade risk checks for Motor 1."""

from __future__ import annotations

from dataclasses import dataclass

from src.math.arbitrage import ArbOpportunity
from src.utils.config import get_settings


@dataclass(frozen=True, slots=True)
class TradeDecision:
    approved: bool
    reason: str
    max_allowed_count: int


class RiskManager:
    """
    STUB — PRE-TRADE RISK MANAGER (INCOMPLETE).

    ⚠️  WARNING: Several checks are NOT yet implemented.
        DO NOT activate TRADING_ENABLED=true until all TODOs below are resolved.
        See KALSHI_BOT_CONTEXT.md section 5 for the full activation checklist.

    Implemented:
    - Capital cap per trade: MAX_TRADE_SIZE_PCT * ACTIVE_CAPITAL_USD
      Real cost = sum of all leg prices (not naive $1/contract assumption).

    TODO OBLIGATORIO antes de TRADING_ENABLED=true:
    - [ ] Check TRADING_ENABLED flag and raise/reject if false
    - [ ] Check BotState.is_paused and reject if paused
    - [ ] Daily/weekly/monthly stop-loss via daily_pnl table
    - [ ] MAX_SIMULTANEOUS_EXPOSURE_PCT against open positions in trades table
    - [ ] DailyPnL write-back after each approved trade
    """

    def check_pre_trade(self, opportunity: ArbOpportunity) -> TradeDecision:
        """
        Validate opportunity against capital caps and return allowed count.

        Args:
            opportunity: Detected arbitrage opportunity from detect_binary_arb /
                         detect_multi_outcome_arb.

        Returns:
            TradeDecision(approved=True) if within capital limits, else approved=False.

        Note:
            Real cost per unit = sum(leg.price_cents for leg in opp.legs), not $1/contract.
            A binary YES+NO arb at 40¢/45¢ costs 85¢/contract, not 100¢.
        """
        settings = get_settings()

        total_cost_per_unit_cents = sum(leg.price_cents for leg in opportunity.legs)
        max_usd = settings.ACTIVE_CAPITAL_USD * (settings.MAX_TRADE_SIZE_PCT / 100.0)
        max_count_by_capital = int(max_usd * 100) // total_cost_per_unit_cents
        allowed_count = min(opportunity.count, max_count_by_capital)

        if allowed_count <= 0:
            return TradeDecision(
                approved=False,
                reason=(
                    f"Capital cap: max_count_by_capital={max_count_by_capital} "
                    f"(max_usd=${max_usd:.2f}, cost_per_unit={total_cost_per_unit_cents}¢)"
                ),
                max_allowed_count=0,
            )

        return TradeDecision(
            approved=True,
            reason="ok",
            max_allowed_count=allowed_count,
        )
