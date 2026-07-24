"""
Fix del loop de code 15 "Action required" en la recovery de OrderbookManagerV2.

Incidente: el get_snapshot de recovery incluía tickers settled → Kalshi respondía code 15 →
el watchdog re-pedía el set completo a ciegas → loop infinito (~6764 fallos/día).

Tres arreglos complementarios, cubiertos acá:
  1. Purga de settled: set_close_times() alimenta close_time; _start_recovery excluye los vencidos
     (y si NO queda ninguno vivo, dispara el circuit breaker en vez de pedir un set vacío).
  2. Manejo de code 15: el error sobre un get_snapshot pendiente limpia la recovery y cuenta el
     fallo, en vez de dejar que el watchdog re-pida ciego.
  3. Circuit breaker: tras N fallos consecutivos, se DESHABILITA la recovery del sid + alerta;
     un éxito posterior lo resetea.
"""

from __future__ import annotations

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


def error_code15(req_id: int, sid_ticker: str | None = None) -> dict:
    msg: dict = {"type": "error", "code": 15, "msg": "Action required", "id": req_id}
    if sid_ticker is not None:
        msg["market_ticker"] = sid_ticker
    return msg


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 200))  # req_ids únicos por comando
    return ws


def _capture_alerts(manager: OrderbookManagerV2) -> list[tuple[str, str]]:
    alerts: list[tuple[str, str]] = []

    async def _fake(kind: str, details: str) -> None:
        alerts.append((kind, details))

    manager._fire_alert = _fake  # type: ignore[method-assign]
    return alerts


# =====================================================
# Fix 1 — purga de settled
# =====================================================


async def test_recovery_purges_settled_ticker(mock_ws):
    """Un ticker con close_time vencido se EXCLUYE del get_snapshot; el vivo se pide igual."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(make_snapshot("LIVE", sid=1, seq=1))
    await manager.handle_message(make_snapshot("DEAD", sid=1, seq=2))
    manager.set_close_times({"DEAD": "2020-01-01T00:00:00Z", "LIVE": "2099-01-01T00:00:00Z"})
    mock_ws.send_command.reset_mock()

    with pytest.raises(SidGapError):
        await manager.handle_message(make_delta("LIVE", sid=1, seq=99))

    _, kwargs = mock_ws.send_command.call_args
    assert kwargs["params"]["market_tickers"] == ["LIVE"]  # DEAD purgado


async def test_recovery_all_settled_trips_breaker(mock_ws):
    """Si TODOS los tickers del sid están settled → circuit breaker, NO se pide snapshot."""
    manager = OrderbookManagerV2(mock_ws)
    alerts = _capture_alerts(manager)
    await manager.handle_message(make_snapshot("DEAD", sid=1, seq=1))
    manager.set_close_times({"DEAD": "2020-01-01T00:00:00Z"})
    mock_ws.send_command.reset_mock()

    with pytest.raises(SidGapError):
        await manager.handle_message(make_delta("DEAD", sid=1, seq=99))

    assert 1 in manager._recovery_disabled_sids
    assert 1 not in manager._recovering
    assert mock_ws.send_command.await_count == 0  # nunca pidió snapshot
    assert any(k == "recovery_disabled" for k, _ in alerts)


def test_set_close_times_ignores_invalid():
    """Un close_time inválido se ignora (ese ticker no se marca unrecoverable por tiempo)."""
    manager = OrderbookManagerV2(AsyncMock())
    manager.set_close_times({"A": "no-es-fecha", "B": None})
    assert manager._is_unrecoverable("A") is False
    assert manager._is_unrecoverable("B") is False


# =====================================================
# Fix 2 — manejo explícito de code 15
# =====================================================


async def test_code15_cleans_up_and_retries_filtered(mock_ws):
    """code 15 sobre el get_snapshot pendiente → limpia la request, cuenta el fallo y reintenta."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(make_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(make_delta("TICK", sid=1, seq=99))
    req_id = manager._pending_req_id_for_sid(1)
    assert req_id is not None
    before = mock_ws.send_command.await_count

    await manager.handle_message(error_code15(req_id))

    assert req_id not in manager._pending_snapshot_requests  # request rechazada, limpiada
    assert manager._recovery_failures_by_sid.get(1) == 1  # fallo contado
    assert mock_ws.send_command.await_count == before + 1  # reintentó (set filtrado)


async def test_code15_logs_close_time_diag(mock_ws):
    """El log del rechazo incluye el diagnóstico de close_time (known + sample) para ver por
    qué la purga no eliminó el ticker (NO_CLOSE_TIME = discovery no lo pasó)."""
    from loguru import logger

    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(make_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(make_delta("TICK", sid=1, seq=99))
    req_id = manager._pending_req_id_for_sid(1)

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="WARNING")
    try:
        await manager.handle_message(error_code15(req_id))
    finally:
        logger.remove(sink)

    blob = "".join(captured)
    line = next(m for m in captured if "v2.recovery_rejected" in m)
    assert "close_times_known=0/1" in line  # TICK sin close_time
    assert "NO_CLOSE_TIME" in blob


async def test_code15_marks_named_ticker_dead(mock_ws):
    """Si el error code 15 nombra un ticker, se marca muerto (no se vuelve a pedir)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(make_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(make_delta("TICK", sid=1, seq=99))
    req_id = manager._pending_req_id_for_sid(1)

    await manager.handle_message(error_code15(req_id, sid_ticker="TICK"))

    assert "TICK" in manager._dead_tickers
    assert manager._is_unrecoverable("TICK") is True


# =====================================================
# Fix 3 — circuit breaker tras N fallos
# =====================================================


async def test_circuit_breaker_disables_after_n_failures(mock_ws):
    """N rechazos code 15 consecutivos → recovery del sid DESHABILITADA + alerta; deja de pedir."""
    manager = OrderbookManagerV2(mock_ws)
    alerts = _capture_alerts(manager)
    await manager.handle_message(make_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(make_delta("TICK", sid=1, seq=99))

    for _ in range(manager.MAX_RECOVERY_FAILURES):
        rid = manager._pending_req_id_for_sid(1)
        assert rid is not None
        await manager.handle_message(error_code15(rid))

    assert 1 in manager._recovery_disabled_sids
    sends = mock_ws.send_command.await_count
    # Un gap nuevo sobre el sid deshabilitado: avanza baseline, NO pide snapshot (loop cortado).
    await manager.handle_message(make_delta("TICK", sid=1, seq=500))
    assert mock_ws.send_command.await_count == sends
    assert any(k == "recovery_disabled" for k, _ in alerts)
    assert any("recovered=" in d for _, d in alerts)  # la alerta incluye el progreso


async def test_successful_recovery_resets_breaker(mock_ws):
    """Un éxito de recovery resetea el contador de fallos (no quedan latcheados)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(make_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(make_delta("TICK", sid=1, seq=99))

    for _ in range(2):  # 2 fallos (< límite)
        rid = manager._pending_req_id_for_sid(1)
        await manager.handle_message(error_code15(rid))
    assert manager._recovery_failures_by_sid.get(1) == 2

    rid = manager._pending_req_id_for_sid(1)
    await manager.handle_message(make_snapshot("TICK", sid=1, seq=200, req_id=rid))

    assert manager._recovery_failures_by_sid.get(1) is None  # reseteado
    assert 1 not in manager._recovering
    assert 1 not in manager._recovery_disabled_sids
