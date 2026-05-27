"""
Regression tests for three V2 bugs fixed in fix(v2): resolve orderbook size=0 desync and seq order.

Test 1: _parse_fp_levels drops size=0 levels (size=0 convention discrepancy fix)
Test 2: seq counter NOT advanced when apply_delta raises (seq ordering fix, normal delta)
Test 3: seq counter NOT advanced when apply_delta raises (seq ordering fix, delta=-6247, production magnitudes)
Test 4: _dispatch logs full traceback via logger.opt(exception=r).error (not NoneType: None)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    SidGapError,
    _parse_fp_levels,
)


def make_v2() -> OrderbookManagerV2:
    ws = MagicMock()
    ws.send_command = AsyncMock(return_value=1)
    return OrderbookManagerV2(ws)


# =====================================================
# Test 1: size=0 levels are dropped by _parse_fp_levels
# =====================================================


def test_parse_fp_levels_filters_size_zero():
    """Snapshot con [price, 0] entre levels válidos: size=0 se filtra antes de apply_snapshot."""
    raw_levels = [["0.4500", "100"], ["0.5000", "0"], ["0.5500", "50"]]
    result = _parse_fp_levels(raw_levels, "KXTEST", "yes")
    prices = [lvl[0] for lvl in result]
    assert 50 not in prices, f"price=50¢ (size=0) debe ser filtrado, got {result}"
    assert 45 in prices
    assert 55 in prices


# =====================================================
# Tests 2 & 3: _last_seq_by_sid not advanced when apply_delta raises
# =====================================================


def _snapshot_msg(ticker: str, sid: int, seq: int, yes_levels=None, no_levels=None) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes": yes_levels or [],
            "no": no_levels or [],
        },
    }


def _delta_msg(ticker: str, sid: int, seq: int, side: str, price: str, delta: str) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "side": side,
            "price": price,
            "delta": delta,
        },
    }


@pytest.mark.asyncio
async def test_delta_negative_on_absent_price_preserves_seq():
    """Después del filter, delta=-X sobre price ausente lanza error y NO avanza last_seq."""
    mgr = make_v2()
    sid = 1
    ticker = "KXTEST"
    mgr._tickers_by_sid[sid] = {ticker}

    # Snapshot: price=50¢ has size=0 → dropped. Book has no level at price=50.
    snap = _snapshot_msg(ticker, sid, seq=100, yes_levels=[["0.4500", "100"], ["0.5000", "0"]])
    await mgr.handle_message(snap)
    assert mgr._last_seq_by_sid[sid] == 100

    # Delta sobre price=50¢ (ausente del book): delta=-200
    delta = _delta_msg(ticker, sid, seq=101, side="yes", price="0.5000", delta="-200")
    from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
    with pytest.raises(OrderbookDesyncError):
        await mgr.handle_message(delta)

    # CRÍTICO: last_seq NO avanzó
    assert mgr._last_seq_by_sid[sid] == 100, (
        f"_last_seq_by_sid advanced to {mgr._last_seq_by_sid[sid]} despite apply raising"
    )


@pytest.mark.asyncio
async def test_regression_production_magnitudes():
    """Reproduce las magnitudes observadas durante el incidente del 25-mayo."""
    mgr = make_v2()
    sid = 1
    ticker = "KXMLB-26-ATH"
    mgr._tickers_by_sid[sid] = {ticker}

    snap = _snapshot_msg(ticker, sid, seq=1000, yes_levels=[["0.0100", "0"]])
    await mgr.handle_message(snap)
    assert mgr._last_seq_by_sid[sid] == 1000

    # Delta con magnitud real observada: -6247 sobre price=1¢
    delta = _delta_msg(ticker, sid, seq=1001, side="yes", price="0.0100", delta="-6247")
    from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
    with pytest.raises(OrderbookDesyncError) as exc_info:
        await mgr.handle_message(delta)

    assert "qty" in str(exc_info.value).lower() or "desync" in str(exc_info.value).lower()
    assert mgr._last_seq_by_sid[sid] == 1000  # seq intacto

    # State del book intacto (no corrupto)
    state = mgr._books[ticker]
    assert state.sequence == 1000


# =====================================================
# Test 4: _dispatch logs full traceback, not NoneType: None
# =====================================================


@pytest.mark.asyncio
async def test_dispatcher_logs_full_stack_trace():
    """Excepción en handler debe loggear con stack trace completo, no NoneType: None.

    loguru does not support exc_info= as a kwarg on .error(); the correct pattern is
    logger.opt(exception=r).error(...). We verify that opt(exception=r) is called with
    the actual exception object — this is what causes loguru to emit the full traceback.
    """
    from src.clients.kalshi_ws import KalshiWebSocket

    captured_calls = []

    class FakeLogger:
        def opt(self, *, exception):
            self._exc = exception
            return self

        def error(self, msg):
            captured_calls.append((self._exc, msg))

        def info(self, *a, **kw): pass
        def debug(self, *a, **kw): pass
        def warning(self, *a, **kw): pass

    ws_client = KalshiWebSocket.__new__(KalshiWebSocket)
    ws_client._handlers = {}
    ws_client._running = False
    ws_client._failure_count = 0
    ws_client._last_connected_at = None

    error = ValueError("test error")

    async def failing_handler(msg):
        raise error

    ws_client._handlers["test_msg"] = [failing_handler]

    msg = {"type": "test_msg", "data": "x"}

    with patch("src.clients.kalshi_ws.logger", FakeLogger()):
        await ws_client._dispatch(msg)

    assert len(captured_calls) == 1, "Expected exactly one error log call"
    exc_arg, log_msg = captured_calls[0]
    # opt(exception=r) must receive the real exception — loguru uses this to emit the traceback
    assert exc_arg is error, (
        f"opt(exception=...) must receive the actual exception object, got {exc_arg!r}"
    )
    assert "test_msg" in log_msg
