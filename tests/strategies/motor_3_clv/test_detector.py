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
