"""
Mutua exclusión por market en el detector de Motor 2 (fix del patrón doble-lado).

Motor 2 es DIRECCIONAL: nunca debe emitir YES y NO del MISMO market. Antes los evaluaba
independiente y emitía ambos cuando yes_ask+no_ask era bajo → abría las dos patas (doble fee,
una liquida en 0). Estos tests fijan el comportamiento nuevo:
  (a) ambos lados con edge y combinado <100 → emite SOLO el de mayor edge;
  (b) combinado >=100 (sin neto real) → descarta AMBOS + log discarded_both_sides;
  (c) un solo lado con edge (mercado normal) → no se ve afectado;
  (d) block_both_sides=False → restaura el comportamiento previo (emite los dos).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from loguru import logger

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import (
    KalshiEventQuotes,
    KalshiQuote,
    _resolve_both_sides,
    _signals_for_outcome,
    find_signals,
)

CAPITAL = 300.0


@contextlib.contextmanager
def _capture_logs():
    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        yield captured
    finally:
        logger.remove(sink)


# =====================================================
# (a) ambos lados con edge, combinado <100 → SOLO el de mayor edge
# =====================================================


def test_both_sides_collapse_to_higher_edge():
    """fair=0.51, yes/no_ask=44 → yes_edge 0.06 > no_edge 0.04, combinado 90 → SOLO YES."""
    q = KalshiQuote("KXMLB-T", "Team", yes_ask_cents=44, no_ask_cents=44)
    out = _signals_for_outcome(q, 0.51, CAPITAL, 0.03)
    assert len(out) == 1
    assert out[0].kalshi_side == "YES"


def test_find_signals_emits_single_side_for_cheap_market():
    """E2E: market 2-way con yes_ask+no_ask<100 y ambos con edge → find_signals emite UNA señal."""
    market = Market(
        key="h2h",
        outcomes=(
            Outcome(name="Los Angeles Lakers", price=2.0),
            Outcome(name="Boston Celtics", price=2.0),
        ),
    )
    odds = OddsEvent(
        id="e1",
        sport_key="basketball_nba",
        commence_time=datetime.now(UTC) + timedelta(hours=2),
        home_team="Los Angeles Lakers",
        away_team="Boston Celtics",
        bookmakers=(Bookmaker(key="pinnacle", title="Pinnacle", markets=(market,)),),
    )
    ke = KalshiEventQuotes(
        event_key="NBA-LAL-BOS",
        outcomes=(
            KalshiQuote("KXNBA-LAL", "Los Angeles Lakers", yes_ask_cents=44, no_ask_cents=44),
            KalshiQuote("KXNBA-BOS", "Boston Celtics", yes_ask_cents=90, no_ask_cents=90),
        ),
    )
    signals = find_signals([ke], [odds], capital_usd=CAPITAL)
    lal = [s for s in signals if s.market_ticker == "KXNBA-LAL"]
    assert len(lal) == 1  # NO ambos lados
    assert lal[0].kalshi_side == "YES"


# =====================================================
# (b) combinado >=100 → descarta AMBOS + log
# =====================================================


def test_resolve_discards_both_when_no_combined_edge():
    """yes_ask+no_ask+fees >= 100 → no hay arbitraje posible → descarta AMBOS + log."""
    q = KalshiQuote("KXMLB-X", "Team", yes_ask_cents=60, no_ask_cents=60)
    out: list = []
    with _capture_logs() as cap:
        _resolve_both_sides(
            out, q, 0.5, yes_edge=0.05, no_edge=0.05, capital_usd=CAPITAL, min_edge=0.03
        )
    assert out == []
    assert any("motor2.signal.discarded_both_sides" in m and "KXMLB-X" in m for m in cap)


# =====================================================
# (c) un solo lado con edge (mercado normal) → sin cambios
# =====================================================


def test_single_side_signal_unaffected():
    """Mercado normal (yes_ask+no_ask=105): solo YES tiene edge → se emite igual que antes."""
    q = KalshiQuote("KXNBA-LAL", "Team", yes_ask_cents=40, no_ask_cents=65)
    out = _signals_for_outcome(q, 0.50, CAPITAL, 0.03)
    assert len(out) == 1
    assert out[0].kalshi_side == "YES"


# =====================================================
# (d) escape hatch: block_both_sides=False restaura el comportamiento previo
# =====================================================


def test_block_disabled_emits_both_sides():
    """Con block_both_sides=False, el detector vuelve a emitir AMBOS lados (comportamiento previo)."""
    q = KalshiQuote("KXMLB-T", "Team", yes_ask_cents=44, no_ask_cents=44)
    out = _signals_for_outcome(q, 0.51, CAPITAL, 0.03, block_both_sides=False)
    assert {s.kalshi_side for s in out} == {"YES", "NO"}
