"""
Tests del Trailing-stop detector (Motor 3, FASE 2) — salida por retroceso pura.

Cubre: trailing solo en ganancia (peak>entry), boundary del retroceso, defensa current>peak,
None no decidible, count<=0, drop<=0; evolución del peak; y la precedencia de decide_exit.
Lógica síncrona/pura (sin red ni await), igual que test_take_profit.py.
"""

from __future__ import annotations

import pytest

from src.storage.models import PortfolioPosition
from src.strategies.motor_3_clv.trailing_stop import (
    DEFAULT_TRAILING_DROP_CENTS,
    decide_exit,
    next_peak_bid,
    trailing_stop_due,
)


def _pos(*, count: int = 10, side: str = "yes") -> PortfolioPosition:
    # El modelo no tiene entry_bid: el entry entra como argumento de trailing_stop_due.
    return PortfolioPosition(ticker="KX", side=side, count=count, close_time=None)


def test_default_drop_is_5():
    assert DEFAULT_TRAILING_DROP_CENTS == 5


# --- trailing_stop_due ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("peak", "current", "entry", "drop", "expected"),
    [
        # En ganancia (peak>entry=60), drop=5:
        (90, 85, 60, 5, True),  # retroceso exacto 90→85 (==peak-drop) → dispara
        (90, 84, 60, 5, True),  # retroceso mayor → dispara
        (90, 86, 60, 5, False),  # retroceso 4 < 5 → aún no
        (90, 90, 60, 5, False),  # sin retroceso (en el pico) → no
        # Nunca estuvo en ganancia (peak<=entry) → no es trailing (sería stop-loss):
        (60, 55, 60, 5, False),  # peak==entry → no se arma
        (58, 50, 60, 5, False),  # peak<entry → no se arma
        # Defensa: current>peak (caller no actualizó el peak) → no disparar con datos incoherentes:
        (90, 95, 60, 5, False),
        # drop<=0 (config inválida) → apagado:
        (90, 80, 60, 0, False),
    ],
)
def test_trailing_stop_due(peak, current, entry, drop, expected):
    assert trailing_stop_due(_pos(), peak, current, entry, drop) is expected


@pytest.mark.parametrize("peak,current,entry", [(None, 85, 60), (90, None, 60), (90, 85, None)])
def test_trailing_stop_due_none_is_not_decidable(peak, current, entry):
    assert trailing_stop_due(_pos(), peak, current, entry) is False


@pytest.mark.parametrize("count", [0, -3])
def test_trailing_stop_due_false_when_no_position(count):
    assert trailing_stop_due(_pos(count=count), 90, 80, 60, 5) is False


def test_trailing_stop_due_uses_default_drop():
    # Sin drop explícito usa DEFAULT=5: 90→85 dispara, 90→86 no.
    assert trailing_stop_due(_pos(), 90, 85, 60) is True
    assert trailing_stop_due(_pos(), 90, 86, 60) is False


# --- next_peak_bid -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("peak", "current", "entry", "expected"),
    [
        (None, 55, 60, 60),  # primer tick, bid<entry → pico arranca en el entry (no protege abajo)
        (None, 72, 60, 72),  # primer tick, bid>entry → pico = bid
        (80, 90, 60, 90),  # bid nuevo máximo → sube el pico
        (80, 75, 60, 80),  # bid baja → el pico se mantiene
        (80, 80, 60, 80),  # bid == pico → estable
    ],
)
def test_next_peak_bid(peak, current, entry, expected):
    assert next_peak_bid(peak, current, entry) == expected


def test_peak_then_trailing_flow():
    """Flujo: sube a 90, retrocede a 84 → con el peak actualizado, el trailing dispara."""
    entry = 60
    peak = next_peak_bid(None, 70, entry)  # 70
    peak = next_peak_bid(peak, 90, entry)  # 90
    peak = next_peak_bid(peak, 84, entry)  # se mantiene 90 (no baja)
    assert peak == 90
    assert trailing_stop_due(_pos(), peak, 84, entry, 5) is True


# --- decide_exit ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tp", "trail", "time", "expected"),
    [
        (True, True, True, "take_profit"),  # TP gana
        (False, True, True, "trailing_stop"),  # trailing > tiempo
        (False, False, True, "time"),
        (False, False, False, None),
    ],
)
def test_decide_exit_precedence(tp, trail, time, expected):
    assert decide_exit(take_profit_due=tp, trailing_due=trail, time_due=time) == expected


# =====================================================
# Fix auditoría 2026-07-01: armado con margen — nunca stop-loss encubierto
# =====================================================


def test_trailing_not_armed_when_peak_barely_above_entry():
    """peak = entry+1 con drop=5 permitía vender hasta entry−4 (stop-loss encubierto,
    contradiciendo el docstring del módulo). El armado exige peak >= entry + drop:
    el precio de disparo (peak − drop) nunca queda debajo del entry."""
    assert (
        trailing_stop_due(_pos(), peak_bid=61, current_bid=56, entry_bid=60, drop_cents=5) is False
    )


def test_trailing_armed_at_exact_margin_triggers_at_breakeven_or_better():
    # peak = entry + drop → precio de disparo = entry (break-even bruto, nunca debajo)
    assert (
        trailing_stop_due(_pos(), peak_bid=65, current_bid=60, entry_bid=60, drop_cents=5) is True
    )
    assert (
        trailing_stop_due(_pos(), peak_bid=64, current_bid=59, entry_bid=60, drop_cents=5) is False
    )
