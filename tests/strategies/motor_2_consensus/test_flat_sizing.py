"""
Sizing FLAT de Motor 2 (MOTOR_2_MAX_STAKE_PCT) vs ¼ Kelly.

Kelly escala el stake con (true_prob − ask): sobre-apuesta donde el edge está sobreestimado
(consenso ruidoso) → asimetría destructiva (sim 141 settled: Kelly −19% ROI vs flat +22.9%).
El sizing flat corta ese acoplamiento: misma fracción del bankroll por trade.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.math.kelly import quarter_kelly_size
from src.strategies.motor_2_consensus.detector import (
    SIZING_MAX_PCT,
    KalshiEventQuotes,
    KalshiQuote,
    _size_usd,
    find_signals,
)

CAPITAL = 4000.0


def test_flat_sizing_returns_fixed_fraction_of_bankroll():
    """pct=1.0 sobre $4000 → stake flat $40.00, sin importar el edge/ask."""
    assert _size_usd(0.58, 47, CAPITAL, max_stake_pct=1.0) == 40.0
    # Mismo stake aunque cambien true_prob y ask (desacoplado del edge):
    assert _size_usd(0.95, 10, CAPITAL, max_stake_pct=1.0) == 40.0


def test_zero_pct_falls_back_to_kelly():
    """pct=0 → ¼ Kelly previo (escala con el edge)."""
    true_prob, ask, capital = 0.60, 50, 4000.0
    expected_count = quarter_kelly_size(
        true_prob, ask, int(capital * 100), kelly_fraction=0.25, max_pct=SIZING_MAX_PCT
    )
    kelly = _size_usd(true_prob, ask, capital, max_stake_pct=0.0)
    assert kelly == round(expected_count * ask / 100.0, 2)
    # Y difiere del flat (prueba que NO es flat por accidente).
    assert kelly != _size_usd(true_prob, ask, capital, max_stake_pct=1.0)


def test_flat_stake_within_risk_caps():
    """El flat default (1%) queda por debajo del cap por trade (5% y MAX_TRADE_SIZE_USD=$200)."""
    flat = _size_usd(0.58, 47, CAPITAL, max_stake_pct=1.0)
    assert flat <= CAPITAL * SIZING_MAX_PCT / 100.0  # <= 5% ($200)
    assert flat <= 200.0  # <= cap absoluto del RiskManager


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


def test_find_signals_threads_flat_sizing():
    """E2E: con max_stake_pct, la señal emitida trae el stake flat (capital × pct/100)."""
    signals = find_signals([_kalshi()], [_odds()], capital_usd=CAPITAL, max_stake_pct=1.0)
    assert len(signals) == 1
    assert signals[0].recommended_size_usd == 40.0  # 1% de $4000, no Kelly


def test_find_signals_default_uses_kelly():
    """Sin max_stake_pct (default 0.0) → sigue el Kelly previo (stake distinto del flat)."""
    signals = find_signals([_kalshi()], [_odds()], capital_usd=CAPITAL)
    assert len(signals) == 1
    assert signals[0].recommended_size_usd != 40.0
