"""
Tope del buffer de bootstrap por ticker — anti-OOM (2026-07-18).

Incidente: crash-loop por OOM (container 1GB al 99.99%, kill cada ~75 min). Causa raíz por
análisis estático: los deltas que llegan antes del snapshot inicial de un ticker se encolaban
en _bootstrap_buffer[ticker] SIN TOPE. Cuando el snapshot inicial nunca llega (el sid grande
cuyo get_snapshot masivo se dropea — mismo incidente que el chunking), el feed live de esos
mercados llena el buffer sin límite → OOM.

Fix: deque(maxlen) por ticker. Descarta los deltas MÁS VIEJOS (que el snapshot, al llegar,
igual descartaría por seq bajo) y preserva los recientes. Lección del repo: nada sin tope.
"""

from __future__ import annotations

import pytest
from loguru import logger

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


class _NoopWs:
    async def send_command(self, *a, **kw) -> int:
        return 1


def _delta(ticker: str, sid: int = 1, seq: int = 2) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.4000",
            "delta_fp": "100.00",
            "side": "yes",
        },
    }


def _snapshot(ticker: str, sid: int = 1, seq: int = 1) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {"market_ticker": ticker, "yes_dollars_fp": [], "no_dollars_fp": []},
    }


@pytest.mark.asyncio
async def test_bootstrap_buffer_is_capped_not_unbounded():
    """MECANISMO anti-OOM: un ticker cuyo snapshot inicial NUNCA llega recibe miles de deltas;
    el buffer NO debe crecer sin límite — se acota al cap, descartando los más viejos."""
    mgr = OrderbookManagerV2(_NoopWs(), bootstrap_buffer_cap=100)
    # 5000 deltas de un ticker que jamás recibió snapshot (book no inicializado).
    for seq in range(2, 5002):
        await mgr.handle_message(_delta("NOSNAP", seq=seq))
    assert len(mgr._bootstrap_buffer["NOSNAP"]) == 100  # capado, no 5000
    assert "NOSNAP" in mgr._bootstrap_capped  # marcado como saturado
    assert mgr.stats()["bootstrap_buffer_msgs"] == 100
    assert mgr.stats()["bootstrap_capped_tickers"] == 1


@pytest.mark.asyncio
async def test_cap_keeps_most_recent_deltas():
    """El deque descarta los VIEJOS y preserva los RECIENTES: cuando el snapshot llegue, los
    seq altos (post-snapshot) son los que importan — los viejos ya están en el snapshot."""
    mgr = OrderbookManagerV2(_NoopWs(), bootstrap_buffer_cap=3)
    for seq in range(2, 8):  # seqs 2..7
        await mgr.handle_message(_delta("T", seq=seq))
    seqs = [m["seq"] for m in mgr._bootstrap_buffer["T"]]
    assert seqs == [5, 6, 7]  # los 3 más recientes, no [2,3,4]


@pytest.mark.asyncio
async def test_cap_logs_once_per_ticker():
    """El warning de saturación es ONE-SHOT por ticker (no spamea cada delta pasado el tope)."""
    mgr = OrderbookManagerV2(_NoopWs(), bootstrap_buffer_cap=10)
    records: list[str] = []
    sink = logger.add(records.append, level="WARNING", format="{message}")
    try:
        for seq in range(2, 40):  # muchos deltas pasado el cap
            await mgr.handle_message(_delta("T", seq=seq))
    finally:
        logger.remove(sink)
    capped_logs = [r for r in records if "v2.bootstrap_buffer_capped" in r and "ticker=T" in r]
    assert len(capped_logs) == 1  # una sola vez, no una por delta


@pytest.mark.asyncio
async def test_snapshot_arrival_clears_capped_flag():
    """Cuando el snapshot inicial POR FIN llega, el ticker sale de _bootstrap_capped (drena)."""
    mgr = OrderbookManagerV2(_NoopWs(), bootstrap_buffer_cap=5)
    for seq in range(2, 30):
        await mgr.handle_message(_delta("T", seq=seq))
    assert "T" in mgr._bootstrap_capped
    await mgr.handle_message(_snapshot("T", seq=100))  # snapshot inicial llega
    assert "T" not in mgr._bootstrap_capped
    assert "T" not in mgr._bootstrap_buffer  # drenado
