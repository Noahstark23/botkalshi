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


def test_404_de_perps_con_control_ok_es_veredicto(capsys):
    """EL CASO REAL del agente (15-ago): /perpetuals, /perpetual_markets y /perps dieron
    404 mientras /exchange/status daba 200 en la MISMA corrida. Con el control positivo
    en verde, el 404 significa 'el recurso no existe acá' — eso SÍ es un veredicto."""

    def _fake(path, timeout=10.0, base=""):
        if path == "/exchange/status":
            return 200, {"exchange_active": True}
        return 404, {"error": "HTTP 404 Not Found"}

    with patch("scripts.sonda_perps._get", side_effect=_fake):
        descubrir()

    salida = capsys.readouterr().out
    assert "control OK" in salida
    assert "NINGÚN endpoint de perps respondió 200" in salida
    assert "sin API no hay motor posible" in salida


def test_host_sin_control_no_produce_veredicto(capsys):
    """LA DISTINCIÓN QUE IMPORTA: si el control TAMBIÉN falla, el host está caído o no
    es el correcto — sus 404 no son concluyentes y el script lo dice en vez de contarlos
    como evidencia de ausencia."""
    with patch("scripts.sonda_perps._get", return_value=(0, {"error": "URLError: timeout"})):
        descubrir()

    salida = capsys.readouterr().out
    assert "los 404 de abajo NO son concluyentes" in salida


def test_reporta_los_endpoints_vivos_con_su_host(capsys):
    """Si alguno responde, se lista con host completo — nadie hereda un path a medias."""

    def _fake(path, timeout=10.0, base=""):
        if path == "/exchange/status":
            return 200, {"exchange_active": True}
        if path == "/margin/markets":
            return 200, {"markets": [], "cursor": ""}
        return 404, {"error": "404"}

    with patch("scripts.sonda_perps._get", side_effect=_fake):
        descubrir()

    salida = capsys.readouterr().out
    assert "ENDPOINTS VIVOS:" in salida
    assert "/margin/markets" in salida


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
