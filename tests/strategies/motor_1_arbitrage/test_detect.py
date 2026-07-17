"""
Tests unitarios de Motor1Engine._detect — matemática del ask sintético + inversión de
profundidad cross-side.

El book V2 mantiene SOLO bids; _detect deriva los asks por complemento (comprar NO ==
vender YES) y usa la profundidad del bid OPUESTO. Estos tests parchean el manager con
BookTop(best_bid=BookLevel(...), best_ask=None) — el shape real del book.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.strategies.motor_1_arbitrage.engine import Motor1Engine
from src.strategies.motor_1_arbitrage.orderbook import BookLevel, BookTop


def _settings(*, min_edge_pct: float = 0.0) -> MagicMock:
    s = MagicMock()
    s.MIN_EDGE_PCT = min_edge_pct
    s.TRADING_ENABLED = False
    s.MOTOR_1_EXECUTION_EDGE_PCT = 0.0
    s.MOTOR_1_MAX_EDGE_PCT = 10_000.0
    return s


def _manager(*, yes_top: BookTop | None, no_top: BookTop | None) -> MagicMock:
    m = MagicMock()

    def _top(ticker: str, side: str) -> BookTop | None:
        return yes_top if side == "yes" else no_top

    m.get_top_of_book.side_effect = _top
    m.tracked_tickers = frozenset({"KXTEST"})
    return m


def _engine(manager: MagicMock, settings: MagicMock) -> Motor1Engine:
    with patch("src.strategies.motor_1_arbitrage.engine.get_settings", return_value=settings):
        return Motor1Engine(manager)


def test_detect_synthetic_ask_math_and_depth_inversion():
    """
    yes_bid=60 (size 80), no_bid=55 (size 30) →
      yes_ask_synth = 100 - 55 = 45  (depth = no_bid.size = 30)
      no_ask_synth  = 100 - 60 = 40  (depth = yes_bid.size = 80)
    Σ = 85 < 100 → arb. count = min(30, 80, 100) = 30.
    """
    yes_top = BookTop(best_bid=BookLevel(price_cents=60, size=80), best_ask=None)
    no_top = BookTop(best_bid=BookLevel(price_cents=55, size=30), best_ask=None)
    eng = _engine(_manager(yes_top=yes_top, no_top=no_top), _settings())

    opp = eng._detect("KXTEST")
    assert opp is not None

    yes_leg = next(leg for leg in opp.legs if leg.side == "yes")
    no_leg = next(leg for leg in opp.legs if leg.side == "no")
    # Precios sintéticos por complemento del bid OPUESTO.
    assert yes_leg.price_cents == 45
    assert no_leg.price_cents == 40
    # Profundidad invertida cross-side: YES ask usa el size del bid NO (30); NO ask usa el
    # size del bid YES (80).
    assert yes_leg.available_size == 30
    assert no_leg.available_size == 80
    # count limitado por la profundidad menor (30).
    assert opp.count == 30


def test_detect_none_when_no_arb():
    """yes_bid=40, no_bid=45 → asks 55 + 60 = 115 >= 100 → sin arb → None."""
    yes_top = BookTop(best_bid=BookLevel(price_cents=40, size=100), best_ask=None)
    no_top = BookTop(best_bid=BookLevel(price_cents=45, size=100), best_ask=None)
    eng = _engine(_manager(yes_top=yes_top, no_top=no_top), _settings())
    assert eng._detect("KXTEST") is None


def test_detect_none_when_yes_bid_missing():
    """Falta el bid YES → None (no se puede derivar el no_ask sintético)."""
    yes_top = BookTop(best_bid=None, best_ask=None)
    no_top = BookTop(best_bid=BookLevel(price_cents=55, size=30), best_ask=None)
    eng = _engine(_manager(yes_top=yes_top, no_top=no_top), _settings())
    assert eng._detect("KXTEST") is None


def test_detect_none_when_no_bid_missing():
    """Falta el bid NO → None."""
    yes_top = BookTop(best_bid=BookLevel(price_cents=60, size=80), best_ask=None)
    no_top = BookTop(best_bid=None, best_ask=None)
    eng = _engine(_manager(yes_top=yes_top, no_top=no_top), _settings())
    assert eng._detect("KXTEST") is None


def test_detect_none_when_top_missing():
    """get_top_of_book devuelve None (ticker stale/uninit) → None."""
    eng = _engine(_manager(yes_top=None, no_top=None), _settings())
    assert eng._detect("KXTEST") is None


def test_detect_guards_synthetic_ask_out_of_range():
    """
    no_bid=0¢ haría yes_ask_synth = 100 → fuera de [1, 99] → None (guard, no ValueError).
    """
    yes_top = BookTop(best_bid=BookLevel(price_cents=60, size=80), best_ask=None)
    no_top = BookTop(best_bid=BookLevel(price_cents=0, size=30), best_ask=None)
    eng = _engine(_manager(yes_top=yes_top, no_top=no_top), _settings())
    assert eng._detect("KXTEST") is None
