"""
Tablero del gate maker — el filtro de contaminación y la regla del n mínimo.

Por qué existe el script (2026-08-14): una lectura ad-hoc del gate mezcló NUEVE filas
del bug del mark congelado (#235 — markout2 no independiente) en el agregado y dio
avg −11.09¢ sobre "n=11" cuando la muestra limpia era n=2. Un veredicto de plata no
puede depender de que quien consulta se acuerde de filtrar.
"""

from __future__ import annotations

import sqlite3

import pytest

from scripts.tablero_gate import CORTE_MARK_CONGELADO, tablero


def _db(tmp_path, filas):
    """filas = [(created_at, markout1, markout2, quote_jump, fee_cents, fee_eff)]"""
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.execute(
        "CREATE TABLE mm_shadow_fills (id INTEGER PRIMARY KEY, created_at TEXT, "
        "markout1_cents REAL, markout2_cents REAL, quote_jump_cents REAL, "
        "fee_cents INT, fee_effective_cents INT, price_cents INT DEFAULT 50)"
    )
    con.executemany(
        "INSERT INTO mm_shadow_fills (created_at, markout1_cents, markout2_cents, "
        "quote_jump_cents, fee_cents, fee_effective_cents) VALUES (?,?,?,?,?,?)",
        filas,
    )
    con.commit()
    con.close()
    return str(p)


def test_excluye_los_fills_del_mark_congelado(tmp_path):
    """EL CASO REAL: 2 filas contaminadas con markout2 = −19 no deben arrastrar el
    promedio de la muestra limpia."""
    db = _db(
        tmp_path,
        [
            ("2026-08-13 23:38", -19.0, -19.0, None, 18, 18),  # pre-#235: contaminada
            ("2026-08-13 23:39", -19.5, -19.5, None, 18, 18),  # pre-#235: contaminada
            ("2026-08-14 22:45", 2.5, -9.0, 0.0, 18, 5),  # limpia
        ],
    )
    salida = tablero(db, half_spread=5)

    assert "CONTAMINADOS (pre-#235" in salida and ": 2 —" in salida
    assert "MUESTRA LIMPIA: n=1" in salida
    assert "-9.00¢" in salida  # el promedio limpio, sin el −19 de las contaminadas


def test_no_declara_veredicto_con_n_chico(tmp_path):
    """La regla que evita el auto-engaño: n=1 no es un resultado."""
    db = _db(tmp_path, [("2026-08-14 22:45", 2.5, -9.0, 0.0, 18, 5)])

    salida = tablero(db, half_spread=5)

    assert "VEREDICTO: SIN DATOS" in salida
    assert "FALLA" not in salida and "PASA" not in salida


def test_segmenta_el_contrafactual_del_blindaje(tmp_path):
    """blindaje@5 = subconjunto con quote_jump < 5; los saltos van aparte."""
    db = _db(
        tmp_path,
        [
            ("2026-08-14 22:45", 2.5, -1.0, 0.0, 18, 5),  # calmo
            ("2026-08-14 22:50", -10.0, -12.0, 16.5, 18, 5),  # salto
            ("2026-08-14 22:55", -3.0, -4.0, None, 18, 5),  # sin etiqueta
        ],
    )
    salida = tablero(db, half_spread=5)

    assert "blindaje@5" in salida and "solo saltos" in salida and "sin etiqueta" in salida
    # cada segmento con su n: 1/1/1
    assert salida.count("n=1 ") >= 3


@pytest.mark.parametrize(
    ("avg2", "esperado"),
    [(-2.0, "PASA"), (-8.0, "FALLA")],
)
def test_umbral_del_gate_con_n_suficiente(tmp_path, avg2, esperado):
    """Con n≥100 el veredicto SÍ se declara, contra −(spread/2) = −5¢."""
    filas = [(f"2026-08-14 22:{i % 60:02d}", avg2, avg2, 0.0, 18, 5) for i in range(105)]
    db = _db(tmp_path, filas)

    salida = tablero(db, half_spread=5)

    assert f"VEREDICTO: {esperado}" in salida


def test_el_corte_es_el_fix_del_mark_congelado():
    """CONTROL de documentación: el corte no es una fecha mágica — es #235."""
    assert CORTE_MARK_CONGELADO.startswith("2026-08-14")


# =====================================================
# Margen teórico — el chequeo PRE-gate (2026-08-14)
# =====================================================
# Bartlett/O'Hara (41,6M trades): el revenue del MM en broad-based (ganador del juego,
# = lo que M5 cotiza) es 0.82¢/contrato, y su fuente NO es el spread sino el sesgo
# conductual del retail. El fee de Kalshi es proporcional a p(1−p) → máximo en 50¢.
# Consecuencia: en la zona media el margen del maker es NEGATIVO antes de la primera
# pérdida por selección adversa. El gate no debe gastar semanas midiendo eso.


def test_margen_negativo_en_la_zona_media():
    from scripts.tablero_gate import margen_teorico

    assert margen_teorico(50) < 0  # la peor: fee round-trip 0.875¢ vs 0.82¢
    assert margen_teorico(45) < 0
    assert margen_teorico(40) < 0


def test_margen_positivo_en_las_colas():
    from scripts.tablero_gate import margen_teorico

    assert margen_teorico(20) > 0.25
    assert margen_teorico(10) > 0.5
    assert margen_teorico(80) == pytest.approx(margen_teorico(20))  # simétrico en p(1−p)


def test_la_zona_muerta_es_el_centro():
    """El conjunto viable es DISJUNTO (dos colas); lo que se reporta es el pozo del
    medio, que es justo donde M5 cotiza ganadores de partido."""
    from scripts.tablero_gate import zona_muerta

    zm = zona_muerta()
    assert zm is not None
    lo, hi = zm
    assert lo <= 50 <= hi  # 50¢ está DENTRO de la zona muerta
    assert lo > 30 and hi < 70  # y el pozo no llega a las colas


def test_el_tablero_avisa_si_la_mayoria_cae_en_zona_muerta(tmp_path):
    """El caso real de M5: ganadores de partido cotizados en 40-60¢."""
    db = _db(
        tmp_path,
        [
            ("2026-08-14 22:45", 1.0, 1.0, 0.0, 18, 5),
            ("2026-08-14 22:46", 1.0, 1.0, 0.0, 18, 5),
        ],
    )
    salida = tablero(db, half_spread=5)

    assert "MARGEN TEÓRICO" in salida
    assert "2/2 (100%) en zona de margen ≤0" in salida
    assert "MAYORÍA EN ZONA MUERTA" in salida
    assert "ZONA MUERTA" in salida
