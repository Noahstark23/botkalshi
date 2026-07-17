"""
scripts/diag_coherence.py — la lógica PURA del estudio de coherencia (Motor 7 paso 0).

Verifica evaluate_pair: detección de violación HARD fillable (con el neto post-fee
exacto), soft a mid, control (mercado coherente no marca nada) y fail-safe (lados
vacíos → par no evaluable).
"""

from __future__ import annotations

from scripts.diag_coherence import _team_of, evaluate_pair
from src.math.fees import kalshi_fee_cents


def _m(ticker: str, bid: int, ask: int) -> dict:
    return {"ticker": ticker, "yes_bid": bid, "yes_ask": ask}


def test_hard_violation_with_exact_net():
    """MECANISMO: ask(etapa)=20 < bid(campeón)=25 → comprar YES etapa (20) + NO campeón
    (100−25=75) cuesta 95¢ con payout ≥ 100¢ → neto = 5 − fees."""
    champ = _m("KXMENWORLDCUP-26-ARG", bid=25, ask=28)
    stage = _m("KXWCSTAGE-26ARG-F", bid=18, ask=20)
    r = evaluate_pair(champ, stage)
    assert r is not None and r["hard"] is True
    fees = kalshi_fee_cents(1, 20) + kalshi_fee_cents(1, 75)
    assert r["net_cents"] == 100 - (20 + 75) - fees


def test_soft_violation_mid_only():
    """Mid campeón (26.5) > mid etapa (24) pero ask etapa (26) ≥ bid campeón (25) → soft,
    NO hard (el spread tapa el fill)."""
    champ = _m("KXMENWORLDCUP-26-ARG", bid=25, ask=28)
    stage = _m("KXWCSTAGE-26ARG-F", bid=22, ask=26)
    r = evaluate_pair(champ, stage)
    assert r is not None and r["soft"] is True and r["hard"] is False


def test_coherent_market_flags_nothing():
    """CONTROL: P(campeón)=25 < P(etapa)=60 (lo normal) → ni soft ni hard."""
    champ = _m("KXMENWORLDCUP-26-ARG", bid=24, ask=26)
    stage = _m("KXWCSTAGE-26ARG-SF", bid=58, ask=62)
    r = evaluate_pair(champ, stage)
    assert r is not None and r["soft"] is False and r["hard"] is False


def test_empty_side_not_evaluable():
    """FAIL-SAFE: un lado sin quote (0/100) → par no evaluable (None), no falso positivo."""
    champ = _m("KXMENWORLDCUP-26-ARG", bid=0, ask=100)
    stage = _m("KXWCSTAGE-26ARG-F", bid=18, ask=20)
    assert evaluate_pair(champ, stage) is None


def test_team_matching_by_last_segment():
    assert _team_of("KXMENWORLDCUP-26-ARG") == "ARG"
    assert _team_of("KXWCSTAGE-26ARG-F") == "F"  # estructura desconocida se VE en el reporte


# =====================================================
# Modo escalera ordinal (--ladder, "Motor 10" 2026-07-13)
# =====================================================


def _lm(
    ticker: str, yes_bid: int, yes_ask: int, no_ask: int, strike: float, event: str = "EV"
) -> dict:
    return {
        "ticker": ticker,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "floor_strike": strike,
        "event_ticker": event,
    }


def test_ladder_hard_violation_correct_legs():
    """MECANISMO con las PATAS CORRECTAS: YES(bajo)@40 + NO(alto)@50 = 90¢ + fees < 100
    → arb garantizado (el medio paga 200, no 0 — la propuesta original venía invertida)."""
    from scripts.diag_coherence import evaluate_ladder_pair

    weak = _lm("KXFIFATOTAL-EV-T1.5", yes_bid=38, yes_ask=40, no_ask=62, strike=1.5)
    strong = _lm("KXFIFATOTAL-EV-T3.5", yes_bid=45, yes_ask=55, no_ask=50, strike=3.5)
    r = evaluate_ladder_pair(weak, strong)
    assert r is not None and r["hard"] is True
    fees = kalshi_fee_cents(1, 40) + kalshi_fee_cents(1, 50)
    assert r["net_cents"] == 100 - (40 + 50) - fees


def test_ladder_coherent_prices_flag_nothing():
    """CONTROL: escalera monótona normal (P(>1.5)=70 > P(>3.5)=20) y costo > 100 → nada."""
    from scripts.diag_coherence import evaluate_ladder_pair

    weak = _lm("T1.5", yes_bid=68, yes_ask=72, no_ask=32, strike=1.5)
    strong = _lm("T3.5", yes_bid=18, yes_ask=22, no_ask=82, strike=3.5)
    r = evaluate_ladder_pair(weak, strong)
    assert r is not None and r["hard"] is False and r["soft"] is False


def test_ladder_soft_when_mid_monotonicity_broken():
    """soft: mid(alto)=60 > mid(bajo)=50 (monotonía rota) pero el costo fillable > 100."""
    from scripts.diag_coherence import evaluate_ladder_pair

    weak = _lm("T1.5", yes_bid=48, yes_ask=52, no_ask=55, strike=1.5)
    strong = _lm("T3.5", yes_bid=58, yes_ask=62, no_ask=48, strike=3.5)
    r = evaluate_ladder_pair(weak, strong)
    assert r is not None and r["soft"] is True and r["hard"] is False


def test_strike_parsing_floor_and_ticker_fallback():
    from scripts.diag_coherence import _strike_of

    assert _strike_of({"floor_strike": 2.5, "ticker": "X"}) == 2.5
    assert _strike_of({"ticker": "KXFIFATOTAL-EV-T3.5"}) == 3.5
    assert _strike_of({"ticker": "KXWCGAME-EV-ARG"}) is None  # sin strike → fuera de escalera
