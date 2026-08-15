"""
Sonda de perps: que el gate se imprima ANTES de los datos y que el parser no invente.

El riesgo de una sonda exploratoria es el auto-engaño: elegir el criterio después de
ver el número, o "arreglar" un parser hasta que devuelva algo lindo. Estos tests fijan
lo contrario — el gate va primero, y sin reference_price la sonda DICE que no puede
medir en vez de inventar un basis.
"""

from __future__ import annotations

from unittest.mock import patch

from scripts.sonda_perps import ROUND_TRIP_TAKER_BPS, _basis_bps, descubrir, muestrear


def test_basis_en_bps_signado_y_simetrico():
    assert _basis_bps(100.5, 100.0) == 50.0  # +50 bps
    assert _basis_bps(99.5, 100.0) == -50.0
    assert _basis_bps(100.0, 0.0) == 0.0  # sin referencia no se divide por cero


def test_descubrir_reporta_los_endpoints_que_fallan(capsys):
    """Si NINGÚN endpoint responde, eso ya es un veredicto (sin API no hay motor)."""
    with patch("scripts.sonda_perps._get", return_value=(0, {"error": "URLError: timeout"})):
        descubrir()

    salida = capsys.readouterr().out
    assert "Si NINGUNO responde 200" in salida
    assert "sin API no hay motor" in salida


def test_el_gate_se_imprime_antes_de_los_datos(capsys):
    """EL ANTI-AUTOENGAÑO: el criterio no se elige después de ver el resultado."""
    with patch("scripts.sonda_perps._get", return_value=(0, None)):
        muestrear("BTCPERP", minutos=0.0, cada_seg=0.0)

    salida = capsys.readouterr().out
    assert salida.index("GATE PRE-REGISTRADO") == 0  # lo PRIMERO que se imprime
    assert f"{ROUND_TRIP_TAKER_BPS:.0f} bps" in salida


def test_sin_reference_price_no_inventa_basis(capsys):
    """Un orderbook sin índice de referencia → la sonda lo DICE. No estima, no rellena."""
    ob = {"orderbook": {"best_bid": "100.0", "best_ask": "100.2"}}
    with patch("scripts.sonda_perps._get", return_value=(200, ob)):
        muestrear("BTCPERP", minutos=0.001, cada_seg=0.0)

    salida = capsys.readouterr().out
    assert "no puede medir basis" in salida
