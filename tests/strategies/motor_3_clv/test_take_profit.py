"""
Tests del Take-Profit detector (Motor 3, FASE 1) — salida por precio pura.

Cubre bid None (no decidible), bid bajo/igual/sobre el umbral, count<=0, y umbral
configurable. Lógica síncrona/pura (sin red ni await), igual que test_detector.py.
"""

from __future__ import annotations

import pytest

from src.storage.models import PortfolioPosition
from src.strategies.motor_3_clv.take_profit import (
    DEFAULT_TAKE_PROFIT_CENTS,
    take_profit_due,
)


def _pos(*, count: int = 10, side: str = "yes") -> PortfolioPosition:
    return PortfolioPosition(ticker="KX", side=side, count=count, close_time=None)


@pytest.mark.parametrize(
    ("bid", "expected"),
    [
        (None, False),  # sin bid resuelto → no decidible
        (89, False),  # bajo el umbral (90) → no
        (90, True),  # borde exacto → dispara
        (91, True),  # sobre el umbral → dispara
        (99, True),  # casi-resuelto → dispara
        (1, False),  # muy lejos → no
    ],
)
def test_take_profit_due_default_threshold(bid, expected):
    assert take_profit_due(_pos(), bid, entry_cents=45) is expected


def test_take_profit_default_threshold_is_90():
    assert DEFAULT_TAKE_PROFIT_CENTS == 90


@pytest.mark.parametrize("count", [0, -5])
def test_take_profit_due_false_when_no_position(count):
    # count<=0 (posición cerrada/inconsistente) → nunca dispara, aunque el bid supere el umbral.
    assert take_profit_due(_pos(count=count), 95, entry_cents=45) is False


@pytest.mark.parametrize(
    ("bid", "threshold", "expected"),
    [
        (80, 80, True),  # umbral custom alcanzado
        (79, 80, False),  # justo bajo el custom
        (95, 99, False),  # umbral más estricto no alcanzado
    ],
)
def test_take_profit_due_custom_threshold(bid, threshold, expected):
    assert take_profit_due(_pos(), bid, threshold, entry_cents=45) is expected


# =====================================================
# Fix auditoría 2026-07-01: el TP debe ASEGURAR GANANCIA, no vender por precio absoluto
# =====================================================


@pytest.mark.parametrize(
    ("entry", "bid", "expected"),
    [
        (93, 91, False),  # entrada CARA (favorito 93c), bid 91 >= umbral → venderla es PÉRDIDA
        (90, 91, False),  # ganancia bruta 1c < fees ida+vuelta (~2c) → neto negativo, no vende
        (45, 91, True),  # ganancia real → asegura
        (88, 95, True),  # bruto 7c > fees → asegura
        (None, 95, False),  # entry desconocido → no decidible (fail-safe: no vender a ciegas)
    ],
)
def test_take_profit_requires_net_profit_vs_entry(entry, bid, expected):
    """El histórico que motivó el TP era 'asegurar lo casi ganado' — nunca auto-liquidar en
    pérdida una entrada comprada por encima del umbral (bug: entradas >=90c se vendían al
    tick siguiente de abrirse)."""
    assert take_profit_due(_pos(), bid, entry_cents=entry) is expected
