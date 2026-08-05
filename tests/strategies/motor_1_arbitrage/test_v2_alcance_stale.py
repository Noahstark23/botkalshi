"""
Alcance del mark_stale por trigger (forense 2026-08-02, resolución 1 segundo).

La película completa, capturada: la siembra re-basea los 191 books → UN desync en UN
ticker llama `_start_recovery(sid)` → el mark_stale SID-WIDE apaga los 191 → la
recovery que lo arreglaría la suprime el rate-limiter anti-espiral → nadie re-basea
hasta el próximo tick de 30s. Con desyncs a 2/min y siembras a 2/min, empatados: los
books vivían 1 segundo de cada 30 (duty cycle 3%).

El fix es de ALCANCE, no de semántica: stale masivo SOLO para el trigger de GAP
(mensajes perdidos = el sid entero es sospechoso). Desync/incoherencia ya pusieron en
cuarentena a SU ticker; la siembra re-basea ciegos. En esos triggers los books
no-divergentes siguen sirviendo — sus mensajes se bufferean durante la recovery y se
drenan al completar.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    SidGapError,
)


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


def _delta(ticker: str, seq: int, sid: int = 1, delta: str = "10.00") -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.50",
            "delta_fp": delta,
            "side": "yes",
        },
    }


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    return ws


async def _manager_con_dos_books(mock_ws) -> OrderbookManagerV2:
    # seam_grace_sec=0: estos tests ejercitan el path de DESYNC REAL (cuarentena +
    # recovery) — la gracia del empalme (2026-08-05) clampearía el underflow y taparía
    # la maquinaria que se está probando. El clamp tiene su propia suite:
    # test_v2_empalme_siembra.py.
    manager = OrderbookManagerV2(mock_ws, seam_grace_sec=0.0)
    await manager.handle_message(_snapshot("SANO", seq=1))
    await manager.handle_message(_snapshot("ROTO", seq=2))
    manager._last_recovery_start_mono.clear()  # arranque limpio para cada test
    return manager


async def test_desync_de_un_ticker_no_ciega_a_los_hermanos(mock_ws):
    """El desync (−500 sobre 100) cuarentena SU ticker y dispara recovery — el hermano
    sano sigue inicializado y SIRVIENDO precios durante la recovery."""
    manager = await _manager_con_dos_books(mock_ws)

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("ROTO", seq=3, delta="-500.00"))

    assert manager._books["ROTO"].is_stale  # el divergido, en cuarentena
    assert not manager._books["SANO"].is_stale  # el hermano NO se tocó
    assert manager._books["SANO"].is_initialized
    assert manager.get_top_of_book("SANO", "yes") is not None  # sigue sirviendo
    assert 1 in manager._recovering  # la recovery del sid arrancó igual


async def test_desync_con_recovery_suprimida_no_ciega_a_los_hermanos(mock_ws):
    """EL SEGUNDO EXACTO del forense (i=191→0 con sp 23→25): desync dentro del intervalo
    mínimo → recovery suprimida. Antes la supresión staleaba los 191; ahora el único
    ciego es el ticker divergido, hasta que la siembra lo re-basee."""
    manager = await _manager_con_dos_books(mock_ws)
    import time as _time

    manager._last_recovery_start_mono[1] = _time.monotonic()  # dentro del intervalo

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("ROTO", seq=3, delta="-500.00"))

    assert manager._recoveries_suppressed == 1  # la recovery se suprimió
    assert manager._books["ROTO"].is_stale  # el divergido, en cuarentena
    assert not manager._books["SANO"].is_stale  # los demás SIGUEN VIVOS (el fix)
    assert manager.get_top_of_book("SANO", "yes") is not None
    assert 1 not in manager._recovering


async def test_gap_sigue_staleando_el_sid_entero(mock_ws):
    """CONTROL: el GAP conserva el stale masivo — mensajes perdidos del sid entero
    hacen sospechosos a TODOS los books (esa seguridad no se negocia)."""
    manager = await _manager_con_dos_books(mock_ws)

    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("SANO", seq=99))

    assert manager._books["SANO"].is_stale
    assert manager._books["ROTO"].is_stale


async def test_gap_suprimido_tambien_stalea_todo(mock_ws):
    """CONTROL: el gap SUPRIMIDO por el rate-limit también stalea el sid entero
    (mejor ciego que fantasma — semántica de #204 intacta para el trigger de gap)."""
    manager = await _manager_con_dos_books(mock_ws)
    import time as _time

    manager._last_recovery_start_mono[1] = _time.monotonic()

    await manager.handle_message(_delta("SANO", seq=99))  # gap suprimido: no raise

    assert manager._recoveries_suppressed == 1
    assert manager._books["SANO"].is_stale
    assert manager._books["ROTO"].is_stale


async def test_siembra_no_apaga_los_books_sanos(mock_ws):
    """La siembra re-basea a los CIEGOS sin blackout de los vivos: el book sano sigue
    sirviendo mientras la recovery de siembra corre (antes: 1 segundo vivo de cada 30)."""
    manager = await _manager_con_dos_books(mock_ws)
    manager._books["ROTO"].mark_stale()  # un ciego para sembrar

    started = await manager.seed_blind_sids()

    assert started == 1
    assert 1 in manager._recovering
    assert not manager._books["SANO"].is_stale  # el sano NO se apagó por la siembra
    assert manager.get_top_of_book("SANO", "yes") is not None
