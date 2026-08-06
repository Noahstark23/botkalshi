"""
Tests del detector (Motor 2) — casos A/B/C de la spec.

Fixtures puras en memoria (payloads de Odds API + quotes de Kalshi, que reemplazan al
"estado de OrderbookManagerV2" — el detector está desacoplado de V2 y recibe los quotes).

  A. Edge BRUTO 3.5% que tras la comisión cae a < 3% → NO se emite señal.
  B. Edge masivo (~33%) → señal emitida con cap 5% ($15) respetado pese a Kelly mayor.
  C. Mismatch de cardinalidad/equipos → el matcher rechaza, el pipeline no colapsa, sin señal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
        # PRE-MATCH: futuro relativo a now → el guardarraíl pre-match no lo saltea
        # (fijo a una fecha pasada haría flakear según el reloj — la misma clase de bug
        # de uptime que ya arreglamos).
        commence_time=datetime.now(UTC) + timedelta(hours=2),
        home_team=home,
        away_team=away,
        bookmakers=(bk,),
    )


def _kalshi_event(*quotes: KalshiQuote) -> KalshiEventQuotes:
    # Prefijo REAL mapeado a basketball (el sport_key del _odds_event de arriba): desde
    # el fail-closed del gate serie↔deporte (2026-08-06), una serie sintética sin entrada
    # en _SERIES_SPORT_PREFIXES ya no matchea — que es el punto del gate.
    # Sin fecha en la clave (como la original "NBA-LAL-BOS"): el gate de fecha no aplica
    # y estos tests siguen midiendo lo suyo (edge/nombres/cardinalidad), no el calendario.
    return KalshiEventQuotes(event_key="KXNBAGAME-LAL-BOS", outcomes=quotes)


def test_case_a_gross_edge_eaten_by_commission_no_signal():
    """Caso A: fair 53.5c, ask 50c → bruto 3.5pp; comisión 1pp → neto 2.5pp < 3pp → SIN señal."""
    # Lakers 1.825 / Celtics 2.10 → fair Lakers ≈ 0.5350.
    odds = _odds_event({"Los Angeles Lakers": 1.825, "Boston Celtics": 2.10})
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=50, no_ask_cents=80),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=80, no_ask_cents=80),
    )
    assert find_signals([ke], [odds], capital_usd=CAPITAL) == []


def test_case_b_large_edge_emits_signal_capped_at_5pct():
    """Caso B: edge grande pero PLAUSIBLE (~11pp) → señal YES; ¼ Kelly pediría >5% pero el
    cap lo fija en $15. (Un edge 'masivo' >15pp ahora es artefacto y se descarta — ver
    test_implausible_edge_discarded.)"""
    # Lakers 1.55 / Celtics 2.5 → fair Lakers ≈ 0.617; ask 50c → edge ≈ 11pp (plausible).
    odds = _odds_event({"Los Angeles Lakers": 1.55, "Boston Celtics": 2.5})
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=50, no_ask_cents=90),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=90, no_ask_cents=90),
    )
    signals = find_signals([ke], [odds], capital_usd=CAPITAL)
    yes = next(s for s in signals if s.kalshi_side == "YES" and s.market_ticker == "KXNBA-LAL")
    assert yes.edge_pct > 0.03
    assert 0.58 < yes.odds_api_fair_prob < 0.66
    # ¼ Kelly daría ~5.85% ($17.5); el cap duro del 5% lo limita estrictamente a $15.
    assert yes.recommended_size_usd <= CAP_USD
    assert yes.recommended_size_usd == 15.0


def test_implausible_edge_discarded():
    """GUARDARRAÍL: un edge monstruoso (>15pp, ej. mercado resuelto/in-play) se DESCARTA."""
    # Lakers 1.05 (fair ≈ 0.93) / Celtics 12.0 ask 40c → edge ≈ 52pp → artefacto → sin señal.
    odds = _odds_event({"Los Angeles Lakers": 1.05, "Boston Celtics": 12.0})
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=40, no_ask_cents=95),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=95, no_ask_cents=95),
    )
    signals = find_signals([ke], [odds], capital_usd=CAPITAL)
    assert all(s.market_ticker != "KXNBA-LAL" or s.kalshi_side != "YES" for s in signals)


def test_diag_funnel_distinguishes_efficient_from_filtered():
    """El embudo diagnóstico puebla started_skip/matched/best_net_edge para leer el 0-señales."""
    # Mercado eficiente: fair ≈ ask → best_edge bajo, 0 señales, pero matched=1.
    odds = _odds_event({"Los Angeles Lakers": 1.95, "Boston Celtics": 1.95})  # fair ≈ 0.50
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=50, no_ask_cents=51),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=51, no_ask_cents=51),
    )
    diag: dict[str, float] = {}
    signals = find_signals([ke], [odds], capital_usd=CAPITAL, diag=diag)
    assert signals == []  # eficiente → sin señal
    assert diag["odds_total"] == 1.0
    assert diag["odds_started_skip"] == 0.0  # pre-match
    assert diag["events_matched"] == 1.0  # SÍ matcheó (no es problema de matching)
    assert diag["best_net_edge"] < 0.03  # best edge por debajo del umbral → mercado eficiente


def test_diag_funnel_counts_started_skips():
    """Un partido ya iniciado cuenta en started_skip y NO en matched (best_edge queda en -1)."""
    odds = _odds_event({"Los Angeles Lakers": 1.55, "Boston Celtics": 2.5})
    started = odds.__class__(
        id=odds.id,
        sport_key=odds.sport_key,
        commence_time=datetime.now(UTC) - timedelta(hours=1),
        home_team=odds.home_team,
        away_team=odds.away_team,
        bookmakers=odds.bookmakers,
    )
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=50, no_ask_cents=90),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=90, no_ask_cents=90),
    )
    diag: dict[str, float] = {}
    assert find_signals([ke], [started], capital_usd=CAPITAL, diag=diag) == []
    assert diag["odds_started_skip"] == 1.0
    assert diag["events_matched"] == 0.0
    assert diag["best_net_edge"] == -1.0  # ningún outcome pre-match evaluado


def test_diag_reject_names_when_team_name_differs():
    """FIXABLE: mismo partido pero un nombre no canoniza igual → reject_names (no 'eficiente')."""
    # Odds dice "LA Lakers" (sin alias) vs Kalshi "Los Angeles Lakers" → set difiere, overlap≥1.
    odds = _odds_event({"LA Lakers": 1.55, "Boston Celtics": 2.5})
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=50, no_ask_cents=90),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=90, no_ask_cents=90),
    )
    diag: dict[str, float] = {}
    assert find_signals([ke], [odds], capital_usd=CAPITAL, diag=diag) == []
    assert diag["events_matched"] == 0.0
    assert diag["reject_names"] == 1.0  # diagnostica BUG de matching, no eficiencia
    assert diag["reject_absent"] == 0.0


def test_diag_reject_absent_when_game_not_in_odds_feed():
    """El partido de Kalshi no está en el feed de odds (sin overlap) → reject_absent (benigno)."""
    odds = _odds_event({"Real Madrid": 1.5, "Barcelona": 2.5}, home="Real Madrid", away="Barcelona")
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=50, no_ask_cents=90),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=90, no_ask_cents=90),
    )
    diag: dict[str, float] = {}
    assert find_signals([ke], [odds], capital_usd=CAPITAL, diag=diag) == []
    assert diag["reject_absent"] == 1.0
    assert diag["reject_names"] == 0.0


def test_diag_reject_cardinality_2way_vs_3way():
    """Kalshi 2-way vs Odds 3-way (mismo partido) → reject_cardinality, no reject_absent."""
    odds = _odds_event(
        {"Argentina": 1.8, "Mexico": 4.5, "Draw": 3.6}, home="Argentina", away="Mexico"
    )
    ke = _kalshi_event(
        KalshiQuote("KX-ARG", "Argentina", yes_ask_cents=40, no_ask_cents=60),
        KalshiQuote("KX-MEX", "Mexico", yes_ask_cents=40, no_ask_cents=60),
    )
    diag: dict[str, float] = {}
    assert find_signals([ke], [odds], capital_usd=CAPITAL, diag=diag) == []
    assert diag["reject_cardinality"] == 1.0
    assert diag["reject_absent"] == 0.0


def test_started_match_skipped_pre_match_guard():
    """GUARDARRAÍL: un partido ya iniciado (commence_time ≤ now) NO se evalúa (spread fantasma)."""
    odds = _odds_event({"Los Angeles Lakers": 1.55, "Boston Celtics": 2.5})
    started = odds.__class__(  # mismo evento pero ya arrancó hace 1h
        id=odds.id,
        sport_key=odds.sport_key,
        commence_time=datetime.now(UTC) - timedelta(hours=1),
        home_team=odds.home_team,
        away_team=odds.away_team,
        bookmakers=odds.bookmakers,
    )
    ke = _kalshi_event(
        KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=50, no_ask_cents=90),
        KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=90, no_ask_cents=90),
    )
    assert find_signals([ke], [started], capital_usd=CAPITAL) == []


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
