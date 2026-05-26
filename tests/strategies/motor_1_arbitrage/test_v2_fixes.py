"""
Regression tests for three V2 bugs fixed in fix(v2): resolve orderbook size=0 desync and seq order.

Test 1: _parse_fp_levels drops size=0 levels (size=0 convention discrepancy fix)
Test 2: seq counter NOT advanced when apply_delta raises (seq ordering fix, normal delta)
Test 3: seq counter NOT advanced when apply_delta raises (seq ordering fix, delta=-6247)
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


def test_parse_fp_levels_drops_size_zero():
    """Level [price=50, size=0] must not appear in the result."""
    raw = [["0.50", "0"], ["0.45", "100"]]
    result = _parse_fp_levels(raw, "TICKER-A", "yes")
    prices = [lvl[0] for lvl in result]
    assert 50 not in prices, f"price=50 (size=0) must be dropped, got {result}"
    assert len(result) == 1
    assert result[0][0] == 45
    assert result[0][1] == 100


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
async def test_seq_not_advanced_after_desync_delta_minus_100():
    """
    Snapshot seq=1 at price=50 absent (size=0, dropped) → delta seq=2 price=50 delta=-100
    → OrderbookDesyncError raised, _last_seq_by_sid[sid] must still be 1 (not 2).
    """
    mgr = make_v2()
    sid = 1
    ticker = "TICKER-A"
    mgr._tickers_by_sid[sid] = {ticker}

    # Snapshot: price=50 has size=0 → dropped by fix. Book has no level at price=50.
    snap = _snapshot_msg(ticker, sid, seq=1, yes_levels=[["0.50", "0"], ["0.45", "200"]])
    await mgr.handle_message(snap)
    assert mgr._last_seq_by_sid[sid] == 1

    # Delta: seq=2, price=50, delta=-100 → qty = 0 + (-100) < 0 → OrderbookDesyncError
    delta = _delta_msg(ticker, sid, seq=2, side="yes", price="0.50", delta="-100")
    from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
    with pytest.raises(OrderbookDesyncError):
        await mgr.handle_message(delta)

    # Seq counter must NOT have advanced to 2
    assert mgr._last_seq_by_sid[sid] == 1, (
        f"_last_seq_by_sid advanced to {mgr._last_seq_by_sid[sid]} despite apply raising"
    )


@pytest.mark.asyncio
async def test_seq_not_advanced_after_desync_delta_minus_6247():
    """
    Same as test above with delta=-6247 (magnitude seen in production logs).
    """
    mgr = make_v2()
    sid = 1
    ticker = "TICKER-B"
    mgr._tickers_by_sid[sid] = {ticker}

    snap = _snapshot_msg(ticker, sid, seq=1, yes_levels=[["0.50", "0"]])
    await mgr.handle_message(snap)
    assert mgr._last_seq_by_sid[sid] == 1

    delta = _delta_msg(ticker, sid, seq=2, side="yes", price="0.50", delta="-6247")
    from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
    with pytest.raises(OrderbookDesyncError):
        await mgr.handle_message(delta)

    assert mgr._last_seq_by_sid[sid] == 1, (
        f"_last_seq_by_sid advanced to {mgr._last_seq_by_sid[sid]} despite apply raising"
    )


# =====================================================
# Test 4: _dispatch logs full traceback, not NoneType: None
# =====================================================


@pytest.mark.asyncio
async def test_dispatch_logs_full_traceback_not_nonetype():
    """
    When a handler raises, _dispatch must log via logger.opt(exception=r).error.
    The log record must contain the exception type, not 'NoneType: None'.
    """
    from src.clients.kalshi_ws import KalshiWebSocket

    captured_calls = []

    class FakeLogger:
        def opt(self, *, exception):
            self._exc = exception
            return self

        def error(self, msg):
            captured_calls.append((self._exc, msg))

        # Stub out other logger methods used during client init
        def info(self, *a, **kw): pass
        def debug(self, *a, **kw): pass
        def warning(self, *a, **kw): pass

    ws_client = KalshiWebSocket.__new__(KalshiWebSocket)
    ws_client._handlers = {}
    ws_client._running = False
    ws_client._failure_count = 0
    ws_client._last_connected_at = None

    error = ValueError("boom")
    async def bad_handler(msg):
        raise error

    ws_client._handlers["orderbook_delta"] = [bad_handler]

    msg = {"type": "orderbook_delta", "sid": 1, "seq": 1, "msg": {}}

    with patch("src.clients.kalshi_ws.logger", FakeLogger()):
        await ws_client._dispatch(msg)

    assert len(captured_calls) == 1, "Expected exactly one error log call"
    exc_arg, log_msg = captured_calls[0]
    assert exc_arg is error, "opt(exception=...) must receive the actual exception object"
    assert "orderbook_delta" in log_msg
