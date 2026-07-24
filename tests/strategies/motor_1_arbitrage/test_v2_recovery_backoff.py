"""
Backoff exponencial del circuit breaker por sid (incidente 2026-07-21).

Antes: un sid deshabilitado (_recovery_disabled_sids) quedaba ciego PERMANENTEMENTE — solo
un redeploy (que borra el estado y repite el ciclo entero) lo "recuperaba". Un sid muerto
por una condición transitoria (futuros que luego abren, feed degradado que se recupera) no
volvía jamás.

Ahora: el disable registra cuándo y cuántas veces seguidas (streak). El próximo gap del sid
tras el cooldown (base·factor^(streak−1), capado: 30s→2min→8min→30min) re-habilita la
recovery UNA ventana (contador de fallos fresco); si vuelve a fracasar, el cooldown crece.
Una recovery OK resetea todo. Dentro del cooldown el gap avanza baseline en silencio (cero
spam); el disable y el reintento loguean UNA línea cada uno.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    SidGapError,
)


def _snapshot(ticker: str, sid: int = 1, seq: int = 1, req_id: int | None = None) -> dict:
    msg: dict = {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {"market_ticker": ticker, "yes_dollars_fp": [], "no_dollars_fp": []},
    }
    if req_id is not None:
        msg["id"] = req_id
    return msg


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


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    return ws


async def _disabled_manager(mock_ws) -> OrderbookManagerV2:
    """Manager con el sid=1 recién deshabilitado por el breaker (vía la ruta real)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=99))
    for _ in range(manager.MAX_RECOVERY_FAILURES):
        rid = manager._pending_req_id_for_sid(1)
        assert rid is not None
        await manager.handle_message(
            {"type": "error", "code": 15, "msg": "Action required", "id": rid}
        )
    assert 1 in manager._recovery_disabled_sids
    return manager


# =====================================================
# (b) Dentro del cooldown: silencio total, cero reintentos
# =====================================================


async def test_disabled_sid_does_not_retry_before_cooldown(mock_ws):
    manager = await _disabled_manager(mock_ws)
    sends = mock_ws.send_command.await_count

    # Varios gaps seguidos DENTRO del cooldown (disabled_at = ahora): baseline avanza, nada más.
    for seq in (500, 600, 700):
        await manager.handle_message(_delta("TICK", sid=1, seq=seq))

    assert mock_ws.send_command.await_count == sends  # cero get_snapshot
    assert 1 in manager._recovery_disabled_sids  # sigue deshabilitado
    assert manager._last_seq_by_sid[1] == 700  # baseline al día (sin falso gap al reintentar)


# =====================================================
# (b') Cooldown vencido: UN reintento re-armado por el próximo gap
# =====================================================


async def test_disabled_sid_retries_after_cooldown(mock_ws):
    manager = await _disabled_manager(mock_ws)
    sends = mock_ws.send_command.await_count
    # Vencer el cooldown (streak=1 → base 30s) retrocediendo el timestamp del disable.
    manager._recovery_disabled_at[1] = time.monotonic() - 31.0

    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=999))

    assert 1 not in manager._recovery_disabled_sids  # re-armado
    assert 1 in manager._recovering  # la recovery ARRANCÓ
    assert mock_ws.send_command.await_count == sends + 1  # pidió el snapshot de nuevo
    assert manager._recovery_failures_by_sid.get(1) is None  # ventana fresca de intentos


async def test_retry_window_failure_redisables_with_longer_cooldown(mock_ws):
    """Si la ventana de reintento también fracasa, se re-deshabilita y el cooldown CRECE
    (streak conservado → exponencial). El loop caliente no puede volver."""
    manager = await _disabled_manager(mock_ws)
    assert manager._disable_streak_by_sid[1] == 1
    delay_1 = manager._backoff_delay_sec(1)

    manager._recovery_disabled_at[1] = time.monotonic() - delay_1 - 1.0
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=999))  # reintento re-armado
    for _ in range(manager.MAX_RECOVERY_FAILURES):  # la ventana entera vuelve a fracasar
        rid = manager._pending_req_id_for_sid(1)
        await manager.handle_message({"type": "error", "code": 15, "msg": "x", "id": rid})

    assert 1 in manager._recovery_disabled_sids  # re-deshabilitado
    assert manager._disable_streak_by_sid[1] == 2  # streak creció
    assert manager._backoff_delay_sec(1) > delay_1  # cooldown más largo


# =====================================================
# (b'') La escala del backoff: exponencial y capado
# =====================================================


def test_backoff_delay_is_exponential_and_capped():
    manager = OrderbookManagerV2(
        AsyncMock(),
        recovery_backoff_base_sec=30.0,
        recovery_backoff_factor=4.0,
        recovery_backoff_cap_sec=1800.0,
    )
    expected = {1: 30.0, 2: 120.0, 3: 480.0, 4: 1800.0, 9: 1800.0}  # 1920→cap, y nunca más
    for streak, delay in expected.items():
        manager._disable_streak_by_sid[7] = streak
        assert manager._backoff_delay_sec(7) == delay


# =====================================================
# Recovery OK resetea el streak (el próximo disable arranca en el cooldown base)
# =====================================================


async def test_successful_recovery_resets_backoff_streak(mock_ws):
    manager = await _disabled_manager(mock_ws)
    manager._recovery_disabled_at[1] = time.monotonic() - 31.0
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=999))  # reintento

    rid = manager._pending_req_id_for_sid(1)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=1500, req_id=rid))  # completa OK

    assert 1 not in manager._recovery_disabled_sids
    assert manager._disable_streak_by_sid.get(1) is None  # streak reseteado
    assert manager._recovery_disabled_at.get(1) is None
    # El book vuelve a SERVIRSE (stale daría None; re-baseado devuelve un BookTop, aunque vacío).
    assert manager.get_top_of_book("TICK", "yes") is not None
    assert not manager._books["TICK"].is_stale


# =====================================================
# Observabilidad: stats expone el countdown del reintento
# =====================================================


async def test_stats_expose_disabled_sids_and_retry_countdown(mock_ws):
    manager = await _disabled_manager(mock_ws)
    s = manager.stats()
    assert s["sids_disabled"] == [1]
    # Recién deshabilitado (streak=1, base 30s): el countdown está en (0, 30].
    assert 0.0 < s["recovery_retry_in_sec"][1] <= 30.0

    manager._recovery_disabled_at[1] = time.monotonic() - 31.0
    assert manager.stats()["recovery_retry_in_sec"][1] == 0.0  # vencido: reintenta al próximo gap
