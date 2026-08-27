"""
Multiplicador maker POR SERIE — el mecanismo, no el valor en disputa.

POR QUÉ EXISTE (2026-08-26): `maker_fee_multiplier_for_ticker` entró en #250 con CERO
tests, en un repo con ~1.600. Decide si el fee de MLB se parte al medio, y con eso si la
zona muerta 38¢-62¢ de `tablero_gate.py` existe o no — o sea, si M5 tiene hábitat en la
banda donde cotiza. Una función de dinero sin cobertura es deuda, y la lección de la fee
~100× subestimada (2026-07-01, invalidó meses de análisis) dice exactamente cuánto cuesta.

⚠️ LO QUE ESTE ARCHIVO NO HACE: no valida que M=0.5 sea el valor CORRECTO para KXMLBGAME.
Ese dato está EN DISPUTA — el código lo afirma desde el 2026-08-07 con timestamp al
milisegundo, pero el PDF oficial que verificamos el 13-ago (POSTERIOR a esa fecha) dice
`KXMLBGAME | 1 | 1`. Hasta que aparezca la fuente del 0.5, pinear ese valor sería fijar
una afirmación sin verificar. Lo que sí se pinea acá es el MECANISMO, que es correcto
gane quien gane la disputa: aislamiento por serie, corte temporal exacto, y manejo de
zonas horarias.

DIRECCIÓN DEL RIESGO, para cuando se resuelva: si el multiplicador real es 1 y usamos
0.5, SUBESTIMAMOS la fee → sobreestimamos la rentabilidad → el gate podría graduar una
estrategia perdedora. El error en esa dirección cuesta plata; en la otra, solo demora.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction

from src.math.fees import maker_fee_multiplier_for_ticker

# El corte que afirma el código (fees.py:34). Se importa como DATO del test, no como
# verdad: si alguien lo corrige, estos tests siguen midiendo el mecanismo correcto.
CORTE = datetime(2026, 8, 7, 4, 59, 45, 131000, tzinfo=UTC)


def test_otras_series_nunca_heredan_el_multiplicador_de_mlb():
    """LA garantía que promete el docstring: el 0.5 de MLB no se derrama a NFL ni a nada.

    Es el control que más importa — aplicar el descuento de una serie a otra
    subestimaría la fee de todo un deporte sin que nadie lo note."""
    despues = CORTE + timedelta(days=30)
    for ticker in (
        "KXNFLGAME-26SEP01-KC",
        "KXNBAGAME-26OCT20-LAL",
        "KXNHLGAME-26OCT10-TOR",
        "KXEPLGAME-26AUG30-ARS",
    ):
        assert maker_fee_multiplier_for_ticker(ticker, as_of=despues) == Fraction(1, 1), ticker


def test_el_pasado_no_se_reescribe():
    """Un fill ANTERIOR al corte conserva el multiplicador que regía entonces.

    Sin esto, un cambio de schedule reescribiría la contabilidad histórica y todo
    análisis previo quedaría medido con una fee que no se pagó."""
    antes = CORTE - timedelta(microseconds=1)
    assert maker_fee_multiplier_for_ticker("KXMLBGAME-26JUL04-NYY", as_of=antes) == Fraction(1, 1)
    # Y bien atrás en el tiempo, por si alguna vez se agrega un corte anterior.
    viejo = CORTE - timedelta(days=200)
    assert maker_fee_multiplier_for_ticker("KXMLBGAME-26JUL04-NYY", as_of=viejo) == Fraction(1, 1)


def test_el_corte_es_exacto_al_microsegundo():
    """El borde se evalúa con >=: el instante EXACTO del corte ya usa el valor nuevo.

    Un corte difuso produce dos contabilidades distintas para el mismo segundo."""
    justo_antes = maker_fee_multiplier_for_ticker(
        "KXMLBGAME-X", as_of=CORTE - timedelta(microseconds=1)
    )
    justo_en = maker_fee_multiplier_for_ticker("KXMLBGAME-X", as_of=CORTE)
    assert justo_antes == Fraction(1, 1)
    assert justo_en != justo_antes, (
        "El corte no cambia nada: o la fecha está mal, o el valor post-corte es 1 "
        "(en cuyo caso la rama entera es código muerto y hay que sacarla)."
    )


def test_naive_se_interpreta_como_utc_y_no_revienta():
    """Convención del repo: hay timestamps NAIVE (settled_at/close_time) y AWARE.

    Mezclarlos lanza TypeError; esta función tiene que absorber el naive como UTC en vez
    de romper el análisis histórico que la llame con una fecha de la DB."""
    naive = CORTE.replace(tzinfo=None)
    assert maker_fee_multiplier_for_ticker("KXMLBGAME-X", as_of=naive) == (
        maker_fee_multiplier_for_ticker("KXMLBGAME-X", as_of=CORTE)
    )


def test_otra_zona_horaria_da_el_mismo_instante():
    """El corte es un INSTANTE, no una hora local: expresarlo en otro huso no lo mueve."""
    en_otro_huso = CORTE.astimezone(timezone(timedelta(hours=-5)))
    assert maker_fee_multiplier_for_ticker("KXMLBGAME-X", as_of=en_otro_huso) == (
        maker_fee_multiplier_for_ticker("KXMLBGAME-X", as_of=CORTE)
    )


def test_la_serie_se_extrae_del_ticker_completo():
    """Los tickers reales traen sufijo de evento/mercado: KXMLBGAME-26AUG27-NYY-DET."""
    largo = maker_fee_multiplier_for_ticker("KXMLBGAME-26AUG27-NYY-DET", as_of=CORTE)
    pelado = maker_fee_multiplier_for_ticker("KXMLBGAME", as_of=CORTE)
    assert largo == pelado


def test_no_es_sensible_a_mayusculas():
    assert maker_fee_multiplier_for_ticker("kxmlbgame-26aug27-nyy", as_of=CORTE) == (
        maker_fee_multiplier_for_ticker("KXMLBGAME-26AUG27-NYY", as_of=CORTE)
    )


def test_serie_desconocida_cae_al_fallback_conservador():
    """Fail-safe direccional: lo que no está verificado paga la fee COMPLETA.

    Subestimar la fee infla la rentabilidad medida — el error caro. El fallback tiene
    que ser 1, jamás un descuento."""
    for ticker in ("SERIE-QUE-NO-EXISTE", "KXFUTURA-27JAN01-X", ""):
        assert maker_fee_multiplier_for_ticker(ticker, as_of=CORTE) == Fraction(1, 1), ticker


def test_sin_as_of_usa_el_ahora():
    """El default no puede ser una fecha fija: un análisis en vivo mide con el hoy."""
    ahora = maker_fee_multiplier_for_ticker("KXMLBGAME-X")
    assert ahora == maker_fee_multiplier_for_ticker("KXMLBGAME-X", as_of=datetime.now(UTC))


def test_el_multiplicador_nunca_supera_uno():
    """Invariante de cordura: M es un DESCUENTO sobre la fee de taker, nunca un recargo.

    Vale para cualquier serie y cualquier fecha; si algún día se agrega una entrada con
    M>1, este test la caza antes de que infle la fee de todo un deporte."""
    fechas = (CORTE - timedelta(days=365), CORTE, CORTE + timedelta(days=365))
    tickers = ("KXMLBGAME-X", "KXNFLGAME-X", "DESCONOCIDA-X")
    for ticker in tickers:
        for cuando in fechas:
            m = maker_fee_multiplier_for_ticker(ticker, as_of=cuando)
            assert Fraction(0) <= m <= Fraction(1), f"{ticker} @ {cuando} → {m}"
