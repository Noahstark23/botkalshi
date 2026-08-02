"""
Siembra explícita de books ciegos (P0 2026-08-02).

Forense del agente web (61h post-#205): initialized=0 sostenido, 99 tickers con el
bootstrap buffer capado y 122k mensajes encolados — M1 mudo (signals=0 en 3.718
heartbeats), M8/M9 sin mid. Causa estructural: NADA pedía el snapshot inicial al
suscribir; los books solo se sembraban si un gap/desync disparaba una recovery, y al
matar los gaps falsos (#205) el gatillo casual desapareció.

La siembra ES una recovery normal (chunking + rate-limit anti-espiral + breaker con
backoff + watchdog de timeout): si Kalshi no responde, el sid se deshabilita con
backoff — no hay loop caliente. `seed_blind_sids()` siembra SOLO sids ciegos (cero
books inicializados); la ceguera parcial la sigue manejando la recovery por gap.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


def _snapshot(ticker: str, sid: int = 1, seq: int = 1, req_id: int | None = None) -> dict:
    msg: dict = {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.50", "100.00"]],
            "no_dollars_fp": [],
        },
    }
    if req_id is not None:
        msg["id"] = req_id
    return msg


def _delta(ticker: str, seq: int, sid: int = 1) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.50",
            "delta_fp": "10.00",
            "side": "yes",
        },
    }


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    return ws


async def test_sid_ciego_se_siembra_via_recovery(mock_ws):
    """EL CASO DEL P0: solo llegan deltas (van al bootstrap buffer, jamás un snapshot).
    Antes: sin gap no había recovery y el sid quedaba ciego para siempre. Ahora la
    siembra pide el get_snapshot explícito."""
    manager = OrderbookManagerV2(mock_ws)
    # El feed registra los tickers del sid vía deltas — ningún book se inicializa.
    await manager.handle_message(_delta("A", seq=1))
    await manager.handle_message(_delta("B", seq=2))
    assert manager.stats()["initialized_tickers"] == 0

    started = await manager.seed_blind_sids()

    assert started == 1
    assert 1 in manager._recovering
    assert mock_ws.send_command.await_count == 1
    _, kwargs = mock_ws.send_command.call_args
    assert kwargs["action"] == "get_snapshot"
    assert set(kwargs["params"]["market_tickers"]) == {"A", "B"}
    assert manager.stats()["seeds_started_total"] == 1

    # El snapshot de respuesta SIEMBRA el book (ciclo completo).
    rid = manager._pending_req_id_for_sid(1)
    await manager.handle_message(_snapshot("A", seq=10, req_id=rid))
    await manager.handle_message(_snapshot("B", seq=11, req_id=rid))
    assert manager.stats()["initialized_tickers"] == 2
    assert 1 not in manager._recovering


async def test_sid_con_algun_book_sano_no_se_siembra(mock_ws):
    """CONTROL: ceguera PARCIAL no dispara siembra (eso lo maneja la recovery por gap) —
    la siembra es para el sid CIEGO, no un re-bootstrap gratuito."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("SANO", seq=1))  # un book inicializado
    await manager.handle_message(_delta("NUEVO", seq=2))  # otro en bootstrap
    mock_ws.send_command.reset_mock()

    started = await manager.seed_blind_sids()

    assert started == 0
    assert mock_ws.send_command.await_count == 0


async def test_sid_en_recovery_o_deshabilitado_no_se_siembra(mock_ws):
    """CONTROL: no pisa una recovery en curso ni ignora el circuit breaker."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_delta("A", seq=1))

    manager._recovering.add(1)
    assert await manager.seed_blind_sids() == 0

    manager._recovering.discard(1)
    manager._recovery_disabled_sids.add(1)
    assert await manager.seed_blind_sids() == 0
    assert mock_ws.send_command.await_count == 0


async def test_siembra_respeta_el_rate_limit_anti_espiral(mock_ws):
    """CONTROL: la siembra pasa por el mismo rate-limit que cualquier recovery — dos
    llamadas seguidas del watchdog no duplican el bootstrap."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_delta("A", seq=1))

    assert await manager.seed_blind_sids() == 1
    # La recovery quedó en curso → el skip es por _recovering; completarla y re-sembrar
    # DENTRO del intervalo mínimo → suprimida por el rate-limit (started=0).
    rid = manager._pending_req_id_for_sid(1)
    await manager.handle_message(_snapshot("A", seq=10, req_id=rid))
    manager._books["A"].mark_stale()  # vuelve a quedar ciego

    assert await manager.seed_blind_sids() == 0  # dentro del intervalo → suprimida
    assert manager.stats()["seeds_started_total"] == 1


async def test_todos_stale_cuenta_como_ciego_y_se_resiembra(mock_ws):
    """El sid cuyos books quedaron TODOS stale (p.ej. supresión anti-espiral) también es
    ciego — la siembra lo re-basea sin esperar el próximo gap casual."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("A", seq=1))
    manager._books["A"].mark_stale()
    manager._last_recovery_start_mono.clear()  # fuera del intervalo mínimo
    mock_ws.send_command.reset_mock()

    started = await manager.seed_blind_sids()

    assert started == 1
    assert 1 in manager._recovering
