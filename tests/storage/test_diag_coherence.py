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
