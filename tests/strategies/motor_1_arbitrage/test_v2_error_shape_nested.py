"""
Shape drift del error WS (incidente 2026-07-23).

Producción mostró errores con el payload ANIDADO:
    {'type': 'error', 'id': N, 'msg': {'code': 15, 'msg': 'Action required'}}
mientras el parser asumía el shape plano ({'code': 15, 'msg': '...'}). Con el anidado,
`raw_msg.get("code")` daba None → el manejo de code 15 sobre un get_snapshot pendiente
JAMÁS disparaba: 351 rechazos/día caían al branch genérico como ruido, la recovery
esperaba snapshots que nunca llegarían → timeout_x5 → breaker → books_initialized=0
(el bloqueante real detrás de "recovered=0/215 nunca parcial": los 5 lotes chunkeados
eran rechazados en ~2ms y nadie se enteraba).

Fix: _parse_error_payload / _error_named_ticker leen AMBOS shapes (anidado con
prioridad). Misma clase de bug que el shape del REST /orderbook (P0 2026-07-19).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.monitoring.health import BotState
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    SidGapError,
    _error_named_ticker,
    _parse_error_payload,
)


def _snapshot(ticker: str, sid: int = 1, seq: int = 1) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {"market_ticker": ticker, "yes_dollars_fp": [], "no_dollars_fp": []},
    }


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


def _nested_error15(req_id: int, ticker: str | None = None) -> dict:
    """El shape REAL observado en producción 2026-07-23 (payload bajo 'msg')."""
    inner: dict = {"code": 15, "msg": "Action required"}
    if ticker is not None:
        inner["market_ticker"] = ticker
    return {"type": "error", "id": req_id, "msg": inner}


@pytest.fixture(autouse=True)
def reset_botstate():
    BotState.last_error = None
    BotState.last_error_at = None
    yield
    BotState.last_error = None
    BotState.last_error_at = None


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    return ws


# =====================================================
# Parser puro: ambos shapes
# =====================================================


def test_parse_error_payload_nested_shape():
    code, text = _parse_error_payload(
        {"type": "error", "id": 7, "msg": {"code": 15, "msg": "Action required"}}
    )
    assert code == 15
    assert text == "Action required"


def test_parse_error_payload_flat_shape_still_works():
    code, text = _parse_error_payload({"type": "error", "code": 15, "msg": "Action required"})
    assert code == 15
    assert text == "Action required"


def test_parse_error_payload_unknown_shape_never_raises():
    assert _parse_error_payload({"type": "error"}) == ("?", "")
    assert _parse_error_payload({"type": "error", "msg": None}) == ("?", "")


def test_error_named_ticker_both_shapes():
    assert _error_named_ticker({"market_ticker": "T1"}) == "T1"
    assert _error_named_ticker({"msg": {"market_ticker": "T2"}}) == "T2"
    assert _error_named_ticker({"msg": {"code": 15}}) is None


# =====================================================
# El caso del incidente: rechazo anidado sobre get_snapshot pendiente
# =====================================================


async def test_nested_code15_triggers_recovery_rejected(mock_ws):
    """El shape anidado DEBE disparar _handle_recovery_rejected (limpia la request, cuenta
    el fallo, reintenta filtrado) — antes caía al branch genérico y la recovery moría por
    timeout 30s con el buffer llenándose (10k msgs descartados por ventana)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=99))
    req_id = manager._pending_req_id_for_sid(1)
    assert req_id is not None
    before = mock_ws.send_command.await_count

    await manager.handle_message(_nested_error15(req_id))

    assert req_id not in manager._pending_snapshot_requests  # request rechazada, limpiada
    assert manager._recovery_failures_by_sid.get(1) == 1  # fallo contado (no timeout ciego)
    assert mock_ws.send_command.await_count == before + 1  # reintentó con el set filtrado


async def test_nested_code15_marks_named_ticker_dead(mock_ws):
    """Si el payload anidado nombra el ticker problemático, se marca dead (no se re-pide)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=99))
    req_id = manager._pending_req_id_for_sid(1)

    await manager.handle_message(_nested_error15(req_id, ticker="TICK"))

    assert "TICK" in manager._dead_tickers
    assert manager._is_unrecoverable("TICK") is True


async def test_chunked_recovery_all_chunks_rejected_nested_breaks_fast(mock_ws):
    """El escenario de producción: 5 lotes chunkeados, TODOS rechazados con el shape anidado
    en ms. Con el fix, cada rechazo cuenta un fallo → el breaker corta en MAX_RECOVERY_FAILURES
    reintentos RÁPIDOS (sin las 5 ventanas de 30s buffereando 10k mensajes)."""
    manager = OrderbookManagerV2(mock_ws, recovery_chunk_size=2)
    for i, t in enumerate(["A", "B", "C", "D"], start=1):
        await manager.handle_message(_snapshot(t, sid=1, seq=i))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("A", sid=1, seq=99))

    # Rechazar el primer lote pendiente de cada intento hasta que el breaker corte.
    for _ in range(manager.MAX_RECOVERY_FAILURES):
        rid = manager._pending_req_id_for_sid(1)
        assert rid is not None
        await manager.handle_message(_nested_error15(rid))

    assert 1 in manager._recovery_disabled_sids  # breaker por rechazos contados, no por timeout
    assert 1 not in manager._recovering  # sin recovery zombie buffereando el feed


async def test_nested_non15_error_goes_to_generic_branch(mock_ws):
    """CONTROL: un error anidado con otro code NO toca la recovery — branch genérico +
    BotState.record_error, sin excepción."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", sid=1, seq=1))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", sid=1, seq=99))
    req_id = manager._pending_req_id_for_sid(1)

    await manager.handle_message({"type": "error", "id": 9999, "msg": {"code": 8, "msg": "x"}})

    assert req_id in manager._pending_snapshot_requests  # la recovery pendiente sigue intacta
    assert BotState.last_error is not None
    assert "code=8" in BotState.last_error
