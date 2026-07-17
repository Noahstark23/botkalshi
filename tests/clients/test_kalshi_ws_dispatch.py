"""
Tests del manejo de excepciones de handlers en KalshiWebSocket._dispatch.

Un gap/desync de orderbook (SidGapError / OrderbookDesyncError, ambos OrderbookError) es
un evento BENIGNO auto-recuperado: el manager ya inició recovery y logueó antes del raise.
_dispatch debe loguearlo limpio (INFO, sin traceback) y NO como ERROR ruidoso. Las
excepciones inesperadas SÍ siguen yendo a ERROR con traceback.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from src.clients.kalshi_ws import KalshiWebSocket
from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import SidGapError


def _make_ws() -> KalshiWebSocket:
    with (
        patch("src.clients.kalshi_ws.KalshiSigner"),
        patch("src.clients.kalshi_ws.get_settings") as gs,
    ):
        s = MagicMock()
        s.KALSHI_PRIVATE_KEY_PATH = "/fake/key.pem"
        s.KALSHI_API_KEY_ID = "test-key"
        s.ws_url = "wss://fake/ws"
        gs.return_value = s
        return KalshiWebSocket()


def _capture_logs() -> tuple[list[tuple[str, str]], int]:
    """Sink loguru → lista de (nivel, mensaje). Devuelve (records, sink_id)."""
    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda m: records.append((m.record["level"].name, m.record["message"])), level="DEBUG"
    )
    return records, sink_id


@pytest.mark.asyncio
async def test_sid_gap_logged_info_not_error():
    """SidGapError de un handler → INFO 'orderbook recovery event', SIN ERROR."""
    ws = _make_ws()

    async def boom(_msg):
        raise SidGapError(sid=1, expected_seq=102, received_seq=105)

    ws.on("orderbook_delta", boom)
    records, sink_id = _capture_logs()
    try:
        await ws._dispatch({"type": "orderbook_delta", "sid": 1})
    finally:
        logger.remove(sink_id)

    levels = [lvl for lvl, _ in records]
    assert "ERROR" not in levels  # ya no es ruido ERROR con traceback
    assert any(lvl == "INFO" and "orderbook recovery event" in msg for lvl, msg in records)


@pytest.mark.asyncio
async def test_orderbook_desync_also_logged_info():
    """OrderbookDesyncError (también OrderbookError) → mismo trato limpio (INFO)."""
    ws = _make_ws()

    async def boom(_msg):
        raise OrderbookDesyncError(ticker="TICKER-A", price_cents=3, new_qty=-11)

    ws.on("orderbook_delta", boom)
    records, sink_id = _capture_logs()
    try:
        await ws._dispatch({"type": "orderbook_delta"})
    finally:
        logger.remove(sink_id)

    assert "ERROR" not in [lvl for lvl, _ in records]
    assert any(lvl == "INFO" and "orderbook recovery event" in msg for lvl, msg in records)


@pytest.mark.asyncio
async def test_unexpected_exception_still_logged_error():
    """Una excepción inesperada (no OrderbookError) SÍ se loguea como ERROR (sin cambios)."""
    ws = _make_ws()

    async def boom(_msg):
        raise RuntimeError("algo inesperado se rompió")

    ws.on("ticker", boom)
    records, sink_id = _capture_logs()
    try:
        await ws._dispatch({"type": "ticker"})
    finally:
        logger.remove(sink_id)

    assert any(lvl == "ERROR" and "Handler exception" in msg for lvl, msg in records)


@pytest.mark.asyncio
async def test_handler_success_logs_nothing():
    """Handler que no revienta → ni INFO de recovery ni ERROR."""
    ws = _make_ws()

    async def ok(_msg):
        return None

    ws.on("ticker", ok)
    records, sink_id = _capture_logs()
    try:
        await ws._dispatch({"type": "ticker"})
    finally:
        logger.remove(sink_id)

    msgs = [msg for _, msg in records]
    assert not any("recovery event" in m or "Handler exception" in m for m in msgs)
