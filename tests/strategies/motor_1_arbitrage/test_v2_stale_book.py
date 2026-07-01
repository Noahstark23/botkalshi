"""
Desync constante de V2: la causa era aplicar deltas sobre un book STALE (marcado durante una
recovery que no completó). Estos tests fijan el fix:
  - un book stale DROPEA los deltas (no los aplica → no desync, no avanza seq);
  - get_top_of_book devuelve None para un book stale (no sirve datos incoherentes);
  - un book NO stale sí aplica (el fix es específico de stale, no rompe el flujo normal).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.return_value = 42
    return ws


def _snapshot(ticker: str = "TICK", sid: int = 1, seq: int = 1) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.50", "100.00"]],  # yes bid @50c qty 100
            "no_dollars_fp": [],
        },
    }


def _delta(seq: int = 2, delta: str = "-500.00") -> dict:
    return {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": seq,
        "msg": {"market_ticker": "TICK", "price_dollars": "0.50", "delta_fp": delta, "side": "yes"},
    }


def test_stale_book_drops_deltas(mock_ws):
    """Un delta que llevaría qty<0 (−500 sobre 100) sobre un book STALE se DROPEA — no desync."""
    m = OrderbookManagerV2(mock_ws)
    m._apply_snapshot_msg(_snapshot())
    book = m._books["TICK"]
    assert book.is_initialized and book.sequence == 1

    book.mark_stale()
    applied = m._apply_delta_msg(_delta())  # sin el fix → OrderbookDesyncError

    assert applied is False  # dropeado, no aplicado
    assert book.sequence == 1  # seq NO avanzó
    assert m.get_top_of_book("TICK", "yes") is None  # stale → no se sirve


def test_get_top_of_book_none_when_stale(mock_ws):
    """get_top_of_book respeta is_stale (el docstring lo prometía; el chequeo faltaba)."""
    m = OrderbookManagerV2(mock_ws)
    m._apply_snapshot_msg(_snapshot())
    assert m.get_top_of_book("TICK", "yes") is not None  # fresco → sirve

    m._books["TICK"].mark_stale()
    assert m.get_top_of_book("TICK", "yes") is None  # stale → None


def test_non_stale_book_applies_delta(mock_ws):
    """Control: sobre un book NO stale el delta SÍ se aplica (el fix es específico de stale)."""
    m = OrderbookManagerV2(mock_ws)
    m._apply_snapshot_msg(_snapshot())
    applied = m._apply_delta_msg(_delta(delta="-50.00"))  # 100−50=50, válido
    assert applied is True
    assert m._books["TICK"].sequence == 2
