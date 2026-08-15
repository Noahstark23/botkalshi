"""Tests del diagnóstico de edad-del-fair + k de M5 (scripts/diag_m5_edad_fair.py).

Lo que se fija acá es el COMPORTAMIENTO DESEADO, no lo observado: el script debe negarse
a concluir con muestra chica (misma disciplina que tablero_gate.py) y debe distinguir el
caso "no hay dispersión en δ" del caso "hay pocos datos" — son dos negativas distintas y
la segunda no se arregla esperando.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from scripts.diag_m5_edad_fair import (
    BUCKETS_MINIMOS_K,
    N_MINIMO_K,
    ajustar_k,
    distancia_al_mid,
    informe,
)


def _db(tmp_path, quotes: list[tuple], fills: list[tuple]) -> str:
    """DB mínima con el shape real de mm_quotes / mm_shadow_fills."""
    ruta = str(tmp_path / "t.db")
    con = sqlite3.connect(ruta)
    con.execute(
        "create table mm_quotes (id integer primary key, ticker text, fair_prob real, "
        "fair_age_sec real, bid_cents int, ask_cents int, size int, yes_bid int, "
        "yes_ask int, inventory int, created_at text)"
    )
    con.execute(
        "create table mm_shadow_fills (id integer primary key, ticker text, side text, "
        "price_cents int, count int, fee_cents int, rule text, markout1_cents real, "
        "markout2_cents real, created_at text)"
    )
    con.executemany(
        "insert into mm_quotes (ticker, fair_prob, fair_age_sec, bid_cents, ask_cents, "
        "size, yes_bid, yes_ask, inventory, created_at) values (?,?,?,?,?,10,?,?,0,?)",
        quotes,
    )
    con.executemany(
        "insert into mm_shadow_fills (ticker, side, price_cents, count, fee_cents, rule, "
        "markout1_cents, markout2_cents, created_at) values (?,?,?,1,0,'x',?,?,?)",
        fills,
    )
    con.commit()
    con.close()
    return ruta


def test_distancia_usa_el_lado_que_lleno():
    """buy = nos llenaron el BID (δ = mid − bid); sell = el ASK (δ = ask − mid)."""
    fila = {"side": "buy", "bid_cents": 45, "ask_cents": 55, "yes_bid": 48, "yes_ask": 52}
    assert distancia_al_mid(fila) == pytest.approx(5.0)  # mid 50 − bid 45
    assert distancia_al_mid({**fila, "side": "sell"}) == pytest.approx(5.0)  # ask 55 − mid 50


def test_distancia_sin_los_dos_lados_del_book_es_none():
    """Sin mid no hay distancia — no se inventa con un solo lado (fail-safe de lectura)."""
    assert (
        distancia_al_mid({"side": "buy", "bid_cents": 45, "yes_bid": 48, "yes_ask": None}) is None
    )


def test_muestra_chica_no_emite_k(tmp_path):
    """La negativa que importa: 3 fills no calibran un k, y el script lo dice."""
    quotes = [("A", 0.5, 10.0, 45, 55, 48, 52, "2026-08-15 10:00:00")]
    fills = [("A", "buy", 45, -1.0, -2.0, "2026-08-15 10:01:00")]
    salida = informe(_db(tmp_path, quotes, fills), None)
    assert "SIN VEREDICTO" in salida
    assert f"/{N_MINIMO_K} fills" in salida


def test_sin_dispersion_en_delta_no_inventa_una_recta():
    """Half-spread fijo → todas las quotes a la misma δ: la recta no está definida.

    Es una negativa DISTINTA de 'pocos datos' (esa se arregla esperando; esta no)."""
    assert ajustar_k([(0.05, 0.01), (0.05, 0.02), (0.05, 0.03)]) is None


def test_ajuste_recupera_un_k_sintetico():
    """Con λ = A·e^(−k·δ) exacto, el ajuste devuelve el k que lo generó."""
    import math

    k_real, a_real = 150.0, 0.5
    puntos = [(d, a_real * math.exp(-k_real * d)) for d in (0.01, 0.02, 0.03, 0.05)]
    ajuste = ajustar_k(puntos)
    assert ajuste is not None
    assert ajuste[0] == pytest.approx(k_real, rel=1e-6)
    assert ajuste[1] == pytest.approx(a_real, rel=1e-6)


def test_menos_buckets_que_el_minimo_no_ajusta():
    assert ajustar_k([(0.02, 0.1), (0.05, 0.01)][: BUCKETS_MINIMOS_K - 1]) is None


def test_edad_del_fair_sale_de_la_quote_previa_del_mismo_ticker(tmp_path):
    """El fill del tick t hereda la edad del fair de la quote del tick t−1 (misma ticker).

    Control: una quote POSTERIOR al fill no debe ganarle a la previa."""
    quotes = [
        ("A", 0.5, 400.0, 45, 55, 48, 52, "2026-08-15 10:00:00"),  # la que llenó
        ("A", 0.5, 10.0, 45, 55, 48, 52, "2026-08-15 10:05:00"),  # posterior: NO cuenta
    ]
    fills = [("A", "buy", 45, -1.0, -9.0, "2026-08-15 10:01:00")]
    salida = informe(_db(tmp_path, quotes, fills), None)
    assert re.search(r"zombie\s+\(325-600s\)\s+n=1\b", salida), salida
    assert re.search(r"zombie\+\s+\(>600s\)\s+n=0\b", salida), salida


def test_ticker_distinto_no_empata_quote(tmp_path):
    """Control: la quote de OTRO ticker no puede prestarle su edad a este fill."""
    quotes = [("B", 0.5, 10.0, 45, 55, 48, 52, "2026-08-15 10:00:00")]
    fills = [("A", "buy", 45, -1.0, -2.0, "2026-08-15 10:01:00")]
    salida = informe(_db(tmp_path, quotes, fills), None)
    assert "fills con quote empatada: 0/1" in salida
