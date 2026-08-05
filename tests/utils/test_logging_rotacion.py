"""
Rotación del log por medianoche O tamaño (2026-08-05, tercera reaparición).

loguru acepta UNA spec de rotation; con "500 MB" a solas el archivo del día no rotaba
al cruzar medianoche y bot_2026-08-04.log terminaba con 1.136 líneas del 05-ago —
todos los greps por día salían mezclados (ya distorsionó dos forenses).
"""

from __future__ import annotations

from datetime import date
from io import BytesIO
from unittest.mock import MagicMock

from src.utils.logging import RotacionDiariaOTamano


def _msg(dia: date) -> MagicMock:
    m = MagicMock()
    m.record = {"time": MagicMock(date=MagicMock(return_value=dia))}
    return m


def _file(size: int) -> BytesIO:
    f = BytesIO(b"x" * size)
    f.seek(size)
    return f


def test_mismo_dia_y_archivo_chico_no_rota():
    rot = RotacionDiariaOTamano(max_bytes=1000)
    dia = date(2026, 8, 4)
    assert rot(_msg(dia), _file(10)) is False
    assert rot(_msg(dia), _file(999)) is False


def test_cruce_de_medianoche_rota():
    """EL CASO DE PRODUCCIÓN: líneas del 05-ago cayendo en bot_2026-08-04.log."""
    rot = RotacionDiariaOTamano(max_bytes=500 * 1024 * 1024)
    assert rot(_msg(date(2026, 8, 4)), _file(10)) is False  # arma el día
    assert rot(_msg(date(2026, 8, 5)), _file(11)) is True  # medianoche → rota
    assert rot(_msg(date(2026, 8, 5)), _file(12)) is False  # y el día nuevo sigue normal


def test_tamano_excedido_rota_sin_esperar_medianoche():
    """El día verboso (400MB/día del incidente 07-31) sigue rotando por tamaño."""
    rot = RotacionDiariaOTamano(max_bytes=1000)
    dia = date(2026, 8, 4)
    assert rot(_msg(dia), _file(10)) is False
    assert rot(_msg(dia), _file(1000)) is True
