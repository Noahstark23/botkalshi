"""
Veredicto de edge shadow (M8 OFI / M6 line-move) — 2026-07-17.

Verifica la lógica PURA de agregación: que el signo del move firmado de M8 traduzca
correctamente a momentum / adverse / ruido (el error que costaría plata sería leer
'reversión' como 'edge comprable'), y que M6 se DIMENSIONE sin fingir un ROI que no mide.
"""

from __future__ import annotations

from scripts.diag_edge_shadow import summarize_linemove, summarize_ofi
from src.storage.models import EdgeWindow


def _ofi(move30: int, move60: int, z: float = 3.5) -> EdgeWindow:
    return EdgeWindow(
        market_ticker="KXMLBGAME-X",
        magnitude_cents=move60,  # T+60 firmado
        gross_spread_cents=move30,  # T+30 firmado
        edge_pct=z,
        kind="ofi",
    )


def _lm(edge_pp: float) -> EdgeWindow:
    return EdgeWindow(
        market_ticker="KXWCGAME-X",
        magnitude_cents=int(edge_pp),
        edge_pct=edge_pp,
        kind="linemove",
    )


def test_ofi_momentum_when_price_follows_pressure():
    """MECANISMO: 25 señales con el precio siguiendo la presión (+3¢ consistente) →
    MOMENTUM significativo (el flujo predice → candidato a F2)."""
    rows = [_ofi(2, 3) for _ in range(25)]
    v = summarize_ofi(rows)
    assert v.n == 25 and v.move60_mean > 0
    assert v.move60_tstat > 2.0
    assert "MOMENTUM" in v.verdict


def test_ofi_adverse_when_price_reverts():
    """CONTROL CRÍTICO (plata): el precio REVIERTE contra la presión (−3¢) → NO es edge
    comprable, es adverse selection. El veredicto NO debe decir momentum."""
    rows = [_ofi(-2, -3) for _ in range(25)]
    v = summarize_ofi(rows)
    assert v.move60_mean < 0
    assert "ADVERSE" in v.verdict or "REVERSIÓN" in v.verdict
    assert "MOMENTUM" not in v.verdict


def test_ofi_noise_when_mean_near_zero():
    """CONTROL: moves que se cancelan (mitad +2, mitad −2) → media ~0 → RUIDO/ARCHIVAR,
    aunque haya muchas señales (no confundir volumen de señal con edge)."""
    rows = [_ofi(2, 2) if i % 2 == 0 else _ofi(-2, -2) for i in range(40)]
    v = summarize_ofi(rows)
    assert abs(v.move60_mean) < 1.0
    assert "RUIDO" in v.verdict or "ARCHIVAR" in v.verdict


def test_ofi_accumulating_below_min_signals():
    """CONTROL: pocas señales → ACUMULANDO, jamás un veredicto direccional sobre 5 datos
    (una media de 5 muestras de flujo ruidoso es suerte, no edge)."""
    rows = [_ofi(2, 3) for _ in range(5)]
    v = summarize_ofi(rows)
    assert "ACUMULANDO" in v.verdict


def test_ofi_no_rows_is_honest():
    v = summarize_ofi([])
    assert v.n == 0 and "SIN DATOS" in v.verdict


def test_linemove_dimensions_without_faking_roi():
    """M6 DIMENSIONA (n + magnitud), no promete rentabilidad: el veredicto cuenta señales
    y la recomendación dice explícito que el ROI real es el cruce con settlements."""
    v = summarize_linemove([_lm(4.0), _lm(6.0), _lm(3.0)])
    assert v.n == 3
    assert "settlement" in v.recommendation.lower()


def test_linemove_zero_is_honest_about_arranque_vs_archivar():
    """CONTROL: 0 señales → distingue 'recién arrancó' de 'archivar' sin afirmar ninguna."""
    v = summarize_linemove([])
    assert v.n == 0 and "SIN SEÑAL" in v.verdict
    assert "arrancó" in v.recommendation or "archivar" in v.recommendation
