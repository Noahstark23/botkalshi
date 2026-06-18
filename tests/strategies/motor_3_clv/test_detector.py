"""
Tests del CLV Detector (Motor 3, FASE 2) — ventana de salida pura.

Cubre close_time pasados, futuros lejanos, y la ventana exacta [28, 30] min, además del
caso close_time=None. La lógica es síncrona/pura (no hay red ni await), así que no se
necesita pytest-asyncio aquí; los tests del poller (async) van con su fase.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.storage.models import PortfolioPosition
from src.strategies.motor_3_clv.detector import (
    CLV_EXIT_CEIL,
    CLV_EXIT_FLOOR,
    clv_exit_due,
    scan_exits,
    summarize_exits,
)

# NAIVE UTC fijo (la convención del Motor 3).
NOW = datetime(2026, 6, 17, 19, 30, 0)


def _pos(ticker: str, *, minutes_to_close: float | None, count: int = 10) -> PortfolioPosition:
    close = None if minutes_to_close is None else NOW + timedelta(minutes=minutes_to_close)
    return PortfolioPosition(ticker=ticker, side="yes", count=count, close_time=close)


@pytest.mark.parametrize(
    ("minutes_to_close", "expected"),
    [
        (29.0, True),  # dentro de [28, 30] → dispara
        (30.0, True),  # borde superior exacto → dispara
        (28.0, True),  # borde inferior exacto → dispara
        (30.1, False),  # aún lejos del cierre (>30) → no
        (27.9, False),  # ya pasó la ventana (<28) → no re-dispara
        (5.0, False),  # muy cerca del cierre pero fuera de ventana → no
        (-1.0, False),  # mercado YA cerrado (remaining negativo) → no
        (120.0, False),  # 2h al cierre → no
    ],
)
def test_clv_exit_due_window(minutes_to_close, expected):
    assert clv_exit_due(_pos("KX", minutes_to_close=minutes_to_close), NOW) is expected


def test_clv_exit_due_close_time_none():
    """Sin close_time (poller aún no lo resolvió) → no se puede decidir → False."""
    assert clv_exit_due(_pos("KX", minutes_to_close=None), NOW) is False


def test_window_constants_sane():
    """El piso (28m) es menor que el techo (30m) — la ventana no está invertida."""
    assert CLV_EXIT_FLOOR < CLV_EXIT_CEIL
    assert timedelta(minutes=30) == CLV_EXIT_CEIL


def test_scan_exits_filters_only_due():
    """scan_exits devuelve SOLO las posiciones en ventana, sin tocar las demás."""
    positions = [
        _pos("A", minutes_to_close=29.0),  # due
        _pos("B", minutes_to_close=45.0),  # lejos
        _pos("C", minutes_to_close=28.5),  # due
        _pos("D", minutes_to_close=None),  # sin close_time
        _pos("E", minutes_to_close=-3.0),  # ya cerrado
    ]
    due = scan_exits(positions, NOW)
    assert {p.ticker for p in due} == {"A", "C"}


def test_summarize_exits_classifies_every_bucket():
    """El diagnóstico cuenta cada posición en exactamente un bucket y reporta totales."""
    positions = [
        _pos("A", minutes_to_close=29.0),  # due
        _pos("B", minutes_to_close=45.0),  # pre-window
        _pos("C", minutes_to_close=28.5),  # due
        _pos("D", minutes_to_close=None),  # sin close_time
        _pos("E", minutes_to_close=-3.0),  # past (ya cerrado)
        _pos("F", minutes_to_close=10.0),  # past (<28)
    ]
    s = summarize_exits(positions, NOW)
    assert s.total == 6
    assert s.with_close_time == 5
    assert s.without_close_time == 1
    assert s.due == 2  # A, C
    assert s.pre_window == 1  # B
    assert s.past_window == 2  # E, F
    # cada posición-con-close_time cae en exactamente un bucket
    assert s.due + s.pre_window + s.past_window == s.with_close_time


def test_summarize_exits_nearest_is_soonest_not_past():
    """La 'próxima salida' es la posición no-pasada con menos tiempo restante."""
    positions = [
        _pos("FAR", minutes_to_close=120.0),
        _pos("SOON", minutes_to_close=31.0),  # aún pre-window pero la más próxima
        _pos("PAST", minutes_to_close=-5.0),  # pasada → no compite
    ]
    s = summarize_exits(positions, NOW)
    assert s.nearest_ticker == "SOON"
    assert s.nearest_remaining_min == pytest.approx(31.0)


def test_summarize_exits_empty_and_none_close_time():
    """Sin posiciones, o todas sin close_time → no hay 'próxima salida'."""
    empty = summarize_exits([], NOW)
    assert empty.total == 0 and empty.nearest_ticker is None

    only_none = summarize_exits([_pos("X", minutes_to_close=None)], NOW)
    assert only_none.without_close_time == 1
    assert only_none.nearest_ticker is None
    assert only_none.nearest_remaining_min is None
