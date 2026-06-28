"""
Watchdog anti-leak de recovery (OrderbookManagerV2).

Escenario que motiva el fix: una recovery que nunca recibe su snapshot (snapshot
perdido en un feed degradado, o un ticker settled que no devuelve snapshot) deja el sid
en _recovering PARA SIEMPRE, y _pending_deltas[sid] crece a la tasa del feed sin tope →
OOM. El bucle es auto-reforzante sobre un droplet que ya sufre gaps de WS.

El watchdog aborta una recovery atascada por TIMEOUT o por TAMAÑO de buffer, descarta lo
encolado (logueando sid + cuántos mensajes), y re-pide el snapshot (buffer/timer frescos).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    SidGapError,
)


def make_snapshot(ticker: str, sid: int = 1, seq: int = 1, req_id: int | None = None) -> dict:
    msg: dict = {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {"market_ticker": ticker, "yes_dollars_fp": [], "no_dollars_fp": []},
    }
    if req_id is not None:
        msg["id"] = req_id
    return msg


def make_delta(ticker: str, sid: int = 1, seq: int = 2) -> dict:
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


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.return_value = 42
    return ws


async def _enter_recovery(manager: OrderbookManagerV2, sid: int = 1) -> None:
    """Lleva al manager a estado _recovering[sid] vía un gap real."""
    await manager.handle_message(make_snapshot("TICK", sid=sid, seq=1))  # baseline seq=1
    with pytest.raises(SidGapError):
        await manager.handle_message(make_delta("TICK", sid=sid, seq=99))  # gap → recovery
    assert sid in manager._recovering


async def test_buffer_overflow_aborts_and_caps(mock_ws):
    """Con max_recovery_buffer chico, el buffer NUNCA supera el tope (no crece sin límite)."""
    manager = OrderbookManagerV2(mock_ws, max_recovery_buffer=3)
    await _enter_recovery(manager)

    # Inundar con mensajes durante la recovery (el snapshot nunca llega).
    for seq in range(100, 140):
        await manager.handle_message(make_delta("TICK", seq=seq))

    # El buffer quedó acotado: jamás superó el tope pese a 40 mensajes.
    assert len(manager._pending_deltas.get(1, [])) <= 3
    # Y siguió pidiendo snapshots (re-trigger en cada abort) → no quedó atascado.
    assert mock_ws.send_command.await_count >= 2


async def test_timeout_aborts_stuck_recovery(mock_ws):
    """Si la recovery excede el timeout, el próximo mensaje la aborta y reintenta."""
    manager = OrderbookManagerV2(mock_ws, recovery_timeout_sec=30.0)
    await _enter_recovery(manager)
    calls_before = mock_ws.send_command.await_count

    # Simular que la recovery arrancó hace 100s (> timeout) sin completar.
    manager._recovery_started_at[1] = time.monotonic() - 100.0
    await manager.handle_message(make_delta("TICK", seq=101))

    # Se reintentó (nuevo get_snapshot) y el timer se reinició (< timeout de nuevo).
    assert mock_ws.send_command.await_count == calls_before + 1
    assert time.monotonic() - manager._recovery_started_at[1] < 1.0


async def test_abort_discards_and_resets_buffer(mock_ws):
    """Al abortar por timeout, el buffer encolado se descarta (no se arrastra)."""
    manager = OrderbookManagerV2(mock_ws, recovery_timeout_sec=30.0)
    await _enter_recovery(manager)
    # Encolar varios mensajes válidos.
    for seq in range(100, 105):
        await manager.handle_message(make_delta("TICK", seq=seq))
    assert len(manager._pending_deltas[1]) >= 5

    # Forzar timeout: el siguiente mensaje aborta → buffer fresco (solo el msg nuevo).
    manager._recovery_started_at[1] = time.monotonic() - 100.0
    await manager.handle_message(make_delta("TICK", seq=200))
    assert len(manager._pending_deltas[1]) == 1


async def test_abort_clears_stale_snapshot_request(mock_ws):
    """Un snapshot tardío del intento abortado NO debe drenar el buffer del intento nuevo."""
    mock_ws.send_command.side_effect = [42, 43]  # req_id viejo, luego nuevo
    manager = OrderbookManagerV2(mock_ws, recovery_timeout_sec=30.0)
    await _enter_recovery(manager)
    assert 42 in manager._pending_snapshot_requests

    manager._recovery_started_at[1] = time.monotonic() - 100.0
    await manager.handle_message(make_delta("TICK", seq=300))

    # El req_id viejo (42) ya no existe; el nuevo (43) sí.
    assert 42 not in manager._pending_snapshot_requests
    assert 43 in manager._pending_snapshot_requests


async def test_abort_logs_recovery_progress(mock_ws):
    """El log de abort reporta recovered=X/N — el diagnóstico clave: ¿llegan los snapshots?
    (recovered creciendo → falta buffer; recovered=0 → no llega ningún snapshot, causa de cuenta)."""
    from loguru import logger

    manager = OrderbookManagerV2(mock_ws, recovery_timeout_sec=30.0)
    await manager.handle_message(make_snapshot("A", sid=1, seq=1))
    await manager.handle_message(make_snapshot("B", sid=1, seq=2))  # sid 1 tiene {A, B}
    with pytest.raises(SidGapError):
        await manager.handle_message(make_delta("A", sid=1, seq=99))  # gap → recovery de A y B
    # Llega el snapshot de recovery de A (req_id=42) → A recuperado, B aún pendiente.
    await manager.handle_message(make_snapshot("A", sid=1, seq=100, req_id=42))

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="WARNING")
    try:
        manager._recovery_started_at[1] = time.monotonic() - 100.0  # forzar timeout
        await manager.handle_message(make_delta("B", sid=1, seq=200))
    finally:
        logger.remove(sink)

    line = next(m for m in captured if "v2.recovery_aborted" in m)
    assert "recovered=1/2" in line  # A recuperó snapshot, B no


async def test_happy_path_recovery_still_completes(mock_ws):
    """Sin atasco, una recovery normal completa y limpia el timer (no rompimos el flujo)."""
    manager = OrderbookManagerV2(mock_ws)
    await _enter_recovery(manager)
    # Llega el snapshot de recovery con el req_id correlacionado (42).
    await manager.handle_message(make_snapshot("TICK", seq=100, req_id=42))
    assert 1 not in manager._recovering
    assert 1 not in manager._recovery_started_at
    assert manager._pending_deltas.get(1, []) == []
