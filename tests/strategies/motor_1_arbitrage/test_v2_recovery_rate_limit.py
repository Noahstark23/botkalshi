"""
Rate-limit anti-espiral de la recovery (incidente 2026-07-31).

La noche del 30→31 el feed corrió con gaps crónicos (100-800/h) que a las ~23:30 se
volvieron 166/min. Cada gap relanzaba un bootstrap del sid ENTERO (~200 tickers): 5
bootstraps por segundo, 38.365 recoveries en 9 horas, y los books no convergían POR
DEFINICIÓN — la recovery era la carga que generaba los gaps siguientes. El bot pasó
9.5 horas con tracked=229 / initialized=0 y /health verde.

Ahora: un gap dentro de `recovery_min_interval_sec` del último ARRANQUE de recovery del
sid NO re-bootstrapea. Suprime: marca los books stale (seguridad idéntica — mejor ciego
que fantasma), avanza el baseline, cuenta la supresión, y NO alerta ni levanta
SidGapError (a 166 gaps/min eso era el flood de Telegram). Los reintentos INTERNOS de
una recovery en curso (retry filtrado tras code 15, abort+restart del watchdog) NO pasan
por el límite: ya están acotados por MAX_RECOVERY_FAILURES + circuit breaker, y
suprimirlos dejaría la recovery muerta hasta el próximo gap casual.
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


async def _manager_tras_una_recovery(mock_ws) -> OrderbookManagerV2:
    """Manager con UNA recovery del sid=1 recién completada (timestamp de arranque fresco)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=99))
    rid = manager._pending_req_id_for_sid(1)
    assert rid is not None
    await manager.handle_message(_snapshot("TICK", sid=1, seq=100, req_id=rid))
    assert 1 not in manager._recovering  # recovery completa; el próximo gap va a _start_recovery
    assert not manager._books["TICK"].is_stale
    return manager


# =====================================================
# Mecanismo: gap dentro del intervalo → supresión total
# =====================================================


async def test_gap_dentro_del_intervalo_se_suprime(mock_ws):
    manager = await _manager_tras_una_recovery(mock_ws)
    sends = mock_ws.send_command.await_count

    # Gap inmediato (< 5s del arranque anterior): NO levanta SidGapError, NO pide snapshot.
    await manager.handle_message(_delta("TICK", sid=1, seq=500))

    assert mock_ws.send_command.await_count == sends  # cero get_snapshot nuevo
    assert 1 not in manager._recovering  # no arrancó recovery
    assert manager._recoveries_suppressed == 1
    assert manager._books["TICK"].is_stale  # mejor ciego que fantasma
    assert manager.get_top_of_book("TICK", "yes") is None  # stale no sirve precios
    assert manager._last_seq_by_sid[1] == 500  # baseline al día (sin falso gap después)


async def test_supresion_no_alimenta_el_contador_de_alertas(mock_ws):
    """El gap suprimido NO entra a _record_gap_and_should_alert — a 166 gaps/min ese
    contador inundó Telegram toda la noche con sid_gap_critical."""
    manager = await _manager_tras_una_recovery(mock_ws)
    gaps_antes = len(manager._gap_timestamps)

    await manager.handle_message(_delta("TICK", sid=1, seq=500))

    assert len(manager._gap_timestamps) == gaps_antes


async def test_intervalo_vencido_arranca_recovery_normal(mock_ws):
    """Pasado el intervalo, el próximo gap vuelve al path completo: recovery + SidGapError."""
    manager = await _manager_tras_una_recovery(mock_ws)
    await manager.handle_message(_delta("TICK", sid=1, seq=500))  # suprimida
    sends = mock_ws.send_command.await_count

    manager._last_recovery_start_mono[1] = time.monotonic() - 6.0  # vencer el intervalo

    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=900))

    assert 1 in manager._recovering
    assert mock_ws.send_command.await_count == sends + 1


# =====================================================
# Los reintentos INTERNOS no pasan por el límite
# =====================================================


async def test_retry_interno_tras_code15_no_se_suprime(mock_ws):
    """El retry filtrado tras un code 15 va DENTRO del intervalo (milisegundos después del
    arranque) y debe re-pedir igual — está acotado por MAX_RECOVERY_FAILURES, no por el
    rate-limit. Suprimirlo dejaría la recovery muerta (sin pending, sin timer)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=99))
    sends = mock_ws.send_command.await_count

    rid = manager._pending_req_id_for_sid(1)
    await manager.handle_message({"type": "error", "code": 15, "msg": "Action required", "id": rid})

    assert mock_ws.send_command.await_count == sends + 1  # re-pidió YA, sin esperar intervalo
    assert 1 in manager._recovering
    assert manager._recoveries_suppressed == 0


async def test_abort_restart_del_watchdog_no_se_suprime(mock_ws):
    """El abort+restart (timeout/buffer_overflow) también es interno: reintenta YA."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=99))
    sends = mock_ws.send_command.await_count

    await manager._abort_and_restart_recovery(1, "buffer_overflow")

    assert mock_ws.send_command.await_count == sends + 1
    assert 1 in manager._recovering
    assert manager._recoveries_suppressed == 0


# =====================================================
# Config: intervalo 0 = comportamiento pre-fix (sin supresión)
# =====================================================


async def test_intervalo_cero_deshabilita_la_supresion(mock_ws):
    manager = OrderbookManagerV2(mock_ws, recovery_min_interval_sec=0.0)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=99))
    rid = manager._pending_req_id_for_sid(1)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=100, req_id=rid))
    sends = mock_ws.send_command.await_count

    with pytest.raises(SidGapError):  # gap inmediato: arranca recovery igual
        await manager.handle_message(_delta("TICK", sid=1, seq=500))

    assert 1 in manager._recovering
    assert mock_ws.send_command.await_count == sends + 1
    assert manager._recoveries_suppressed == 0


# =====================================================
# Observabilidad
# =====================================================


async def test_stats_exponen_supresiones(mock_ws):
    manager = await _manager_tras_una_recovery(mock_ws)
    assert manager.stats()["recoveries_suppressed_total"] == 0

    await manager.handle_message(_delta("TICK", sid=1, seq=500))
    await manager.handle_message(_delta("TICK", sid=1, seq=600))

    assert manager.stats()["recoveries_suppressed_total"] == 2
