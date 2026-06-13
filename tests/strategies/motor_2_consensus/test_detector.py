"""
Tests del detector (Motor 2) — casos A/B/C de la spec.

Fixtures puras en memoria (payloads de Odds API + quotes de Kalshi, que reemplazan al
"estado de OrderbookManagerV2" — el detector está desacoplado de V2 y recibe los quotes).

  A. Edge BRUTO 3.5% que tras la comisión cae a < 3% → NO se emite señal.
  B. Edge masivo (~33%) → señal emitida con cap 5% ($15) respetado pese a Kelly mayor.
  C. Mismatch de cardinalidad/equipos → el matcher rechaza, el pipeline no colapsa, sin señal.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import (
    KalshiEventQuotes,
    KalshiQuote,
    find_signals,
)

CAPITAL = 300.0
CAP_USD = CAPITAL * 0.05  # 5% = $15.00


def _odds_event(
    h2h: dict[str, float], *, home="Los Angeles Lakers", away="Boston Celtics"
) -> OddsEvent:
    market = Market(key="h2h", outcomes=tuple(Outcome(name=n, price=p) for n, p in h2h.items()))
    bk = Bookmaker(key="pinnacle", title="Pinnacle", markets=(market,))
    return OddsEvent(
        id="e1",
        sport_key="basketball_nba",
        commence_time=datetime(2026, 6, 12, 18, tzinfo=UTC),
        home_team=home,
        away_team=away,
        bookmakers=(bk,),
    )


def _kalshi_event(*quotes: KalshiQuote) -> KalshiEventQuotes:
    return KalshiEventQuotes(event_key="NBA-LAL-BOS", outcomes=quotes)


def test_case_a_gross_edge_eaten_by_commission_no_signal():
    """Caso A: fair 53.5c, ask 50c → bruto 3.5pp; comisión 1pp → neto 2.5pp < 3pp → SIN señal."""
    # Lakers 1.825 / Celtics 2.10 → fair Lakers ≈ 0.5350.
    odds = _odds_event({"Los Angeles Lakers": 1.825, "Boston Celtics": 2.10})
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=50, no_ask_cents=80),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=80, no_ask_cents=80),
    )
    assert find_signals([ke], [odds], capital_usd=CAPITAL) == []


def test_case_b_massive_edge_emits_signal_capped_at_5pct():
    """Caso B: edge masivo → señal YES; ¼ Kelly pediría >5% pero el cap lo fija en $15."""
    # Lakers 1.20 / Celtics 6.0 → fair Lakers ≈ 0.833; ask 50c → edge enorme.
    odds = _odds_event({"Los Angeles Lakers": 1.20, "Boston Celtics": 6.0})
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=50, no_ask_cents=90),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=90, no_ask_cents=90),
    )
    signals = find_signals([ke], [odds], capital_usd=CAPITAL)
    yes = next(s for s in signals if s.kalshi_side == "YES" and s.market_ticker == "KXNBA-LAL")
    assert yes.edge_pct > 0.03
    assert 0.80 < yes.odds_api_fair_prob < 0.85
    # ¼ Kelly daría ~16.7% ($50); el cap duro del 5% lo limita estrictamente a $15.
    assert yes.recommended_size_usd <= CAP_USD
    assert yes.recommended_size_usd == 15.0


def test_case_c_matcher_mismatch_no_signal_no_crash():
    """Caso C: Kalshi 2-way vs Odds API 3-way (1X2) → matcher falla → sin señal, sin crash."""
    odds_3way = _odds_event(
        {"Argentina": 1.80, "Mexico": 4.50, "Draw": 3.60}, home="Argentina", away="Mexico"
    )
    ke_2way = _kalshi_event(
        KalshiQuote("KX-ARG", "Argentina", yes_ask_cents=40, no_ask_cents=60),
        KalshiQuote("KX-MEX", "Mexico", yes_ask_cents=40, no_ask_cents=60),
    )
    assert find_signals([ke_2way], [odds_3way], capital_usd=CAPITAL) == []


def test_accent_folding_flows_through_detector():
    """Extra: el hotfix de acentos fluye end-to-end (odds 'México' matchea kalshi 'Mexico')."""
    odds_3way = _odds_event(
        {"Argentina": 1.80, "México": 4.50, "Draw": 3.60}, home="Argentina", away="México"
    )
    ke_3way = _kalshi_event(
        KalshiQuote("KX-ARG", "Argentina", yes_ask_cents=40, no_ask_cents=70),
        KalshiQuote("KX-MEX", "Mexico", yes_ask_cents=40, no_ask_cents=70),
        KalshiQuote("KX-DRAW", "Draw", yes_ask_cents=40, no_ask_cents=70),
    )
    signals = find_signals([ke_3way], [odds_3way], capital_usd=CAPITAL)
    assert any(s.market_ticker == "KX-ARG" and s.kalshi_side == "YES" for s in signals)
