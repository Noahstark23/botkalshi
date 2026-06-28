"""
El poller de Motor 2 dimensiona contra el capital EFECTIVO del RiskManager (dinámico) por
ciclo, no contra el ACTIVE_CAPITAL_USD estático. Refleja depósitos/retiros + el cash real
sin tocar variables.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import KalshiEventQuotes, KalshiQuote
from src.strategies.motor_2_consensus.poller import Motor2ShadowPoller


class _Src:
    """Fuente fake (kalshi u odds): devuelve una lista fija; is_live configurable."""

    def __init__(self, items: list, is_live: bool = False):
        self._items = items
        self.is_live = is_live

    async def fetch(self) -> list:
        return self._items


def _odds() -> OddsEvent:
    market = Market(
        key="h2h",
        outcomes=(
            Outcome(name="Philadelphia Phillies", price=1.72),
            Outcome(name="New York Mets", price=2.38),
        ),
    )
    return OddsEvent(
        id="phinym",
        sport_key="baseball_mlb",
        commence_time=datetime.now(UTC) + timedelta(hours=2),
        home_team="Philadelphia Phillies",
        away_team="New York Mets",
        bookmakers=(Bookmaker(key="pinnacle", title="Pinnacle", markets=(market,)),),
    )


def _kalshi() -> KalshiEventQuotes:
    return KalshiEventQuotes(
        event_key="KXMLBGAME-PHINYM",
        outcomes=(
            KalshiQuote("KXMLB-PHI", "Philadelphia Phillies", yes_ask_cents=48, no_ask_cents=90),
            KalshiQuote("KXMLB-NYM", "New York Mets", yes_ask_cents=90, no_ask_cents=90),
        ),
    )


def test_capital_for_cycle_uses_risk_manager():
    """Con RiskManager → el capital del ciclo es su capital efectivo (dinámico)."""
    rm = MagicMock()
    rm.effective_capital_usd.return_value = 1234.0
    poller = Motor2ShadowPoller(_Src([]), _Src([]), capital_usd=300.0, risk_manager=rm)
    assert poller._capital_for_cycle() == 1234.0


def test_capital_for_cycle_fallback_static():
    """Sin RiskManager → cae al capital estático (tests / shadow sin RM)."""
    poller = Motor2ShadowPoller(_Src([]), _Src([]), capital_usd=300.0)
    assert poller._capital_for_cycle() == 300.0


@pytest.mark.asyncio
async def test_poll_once_sizes_off_effective_capital():
    """E2E: el sizing flat usa el capital EFECTIVO del RM ($4000 → 1% = $40), no el estático."""
    rm = MagicMock()
    rm.effective_capital_usd.return_value = 4000.0
    poller = Motor2ShadowPoller(
        _Src([_kalshi()]),
        _Src([_odds()], is_live=False),
        capital_usd=300.0,  # estático distinto → si se usara, el size sería otro
        max_stake_pct=1.0,
        risk_manager=rm,
    )
    signals = await poller.poll_once()
    assert len(signals) == 1
    assert signals[0].recommended_size_usd == 40.0  # 1% de $4000 (efectivo), no de $300
    rm.effective_capital_usd.assert_called()  # se consultó al RM en el ciclo
