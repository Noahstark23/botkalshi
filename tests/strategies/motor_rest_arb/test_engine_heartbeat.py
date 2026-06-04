"""
Tests del heartbeat de observabilidad del RestArbEngine.

El heartbeat distingue "0 edges porque el mercado es eficiente" (normal) de
"motor zombi que no procesa" — emite un log INFO cada HEARTBEAT_EVERY tickers.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.strategies.motor_rest_arb.engine import RestArbEngine


def _engine() -> RestArbEngine:
    settings = MagicMock()
    settings.MOTOR_REST_MIN_EDGE_CENTS = 1
    settings.MOTOR_REST_MIN_DEPTH = 2
    with patch("src.strategies.motor_rest_arb.engine.get_settings", return_value=settings):
        return RestArbEngine()


def _non_arb_ticker() -> dict:
    """Ticker con bid/ask normal (sin cruce) — no graba EdgeWindow."""
    return {
        "type": "ticker",
        "msg": {
            "market_ticker": "KXNBA-26-NYK",
            "yes_bid_dollars": "0.5300",
            "yes_ask_dollars": "0.5400",
            "yes_bid_size_fp": "685437.20",
            "yes_ask_size_fp": "663016.15",
        },
    }


@pytest.mark.asyncio
async def test_heartbeat_logs_every_n_tickers():
    """A los HEARTBEAT_EVERY tickers evaluados se emite un log INFO de heartbeat."""
    from loguru import logger

    eng = _engine()
    eng.HEARTBEAT_EVERY = 5  # bajar el umbral para el test
    records: list[str] = []
    sink = logger.add(records.append, level="INFO", format="{message}")
    try:
        for _ in range(5):
            await eng.on_ticker(_non_arb_ticker())
    finally:
        logger.remove(sink)

    assert eng._tickers_evaluated == 5
    assert eng._signals_seen == 0  # ningún cruce (mercado eficiente)
    hb = [r for r in records if "REST Engine Heartbeat" in r]
    assert len(hb) == 1
    assert "5 tickers evaluados" in hb[0]
    assert "0 cruces detectados" in hb[0]


@pytest.mark.asyncio
async def test_no_heartbeat_before_threshold():
    """Antes de HEARTBEAT_EVERY no se emite heartbeat (no spamea por cada ticker)."""
    from loguru import logger

    eng = _engine()
    eng.HEARTBEAT_EVERY = 200
    records: list[str] = []
    sink = logger.add(records.append, level="INFO", format="{message}")
    try:
        for _ in range(10):
            await eng.on_ticker(_non_arb_ticker())
    finally:
        logger.remove(sink)

    assert eng._tickers_evaluated == 10
    assert not any("REST Engine Heartbeat" in r for r in records)
