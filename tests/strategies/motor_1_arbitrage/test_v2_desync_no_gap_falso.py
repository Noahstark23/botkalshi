"""
El desync no puede generar un GAP FALSO (2026-08-04, último eslabón de la cadena).

Firma en producción (post-#209, duty cycle 79%): las supresiones salían en PARES a
1-30ms de distancia — la primera con `stale_all=False` (el desync) y la segunda con
`stale_all=True` (un "gap"). No eran ráfagas del feed: era causalidad. El `raise` del
except de desync sale de `handle_message` ANTES del avance de baseline, así que el
baseline quedaba congelado y el mensaje SIGUIENTE — perfectamente en secuencia — se
leía como gap → mass-stale de los 211 books.

Es el mismo bug que #205 mató para el camino "book stale dropea el delta"; el camino de
la excepción se escapaba por el `raise`. Con esto, la mitad de los gaps (los
autoinfligidos) desaparecen y con ellos su mass-stale.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    SidGapError,
)


def _snapshot(ticker: str, sid: int = 1, seq: int = 1) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.50", "100.00"]],
            "no_dollars_fp": [],
        },
    }


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


async def _manager_con_desync(mock_ws) -> OrderbookManagerV2:
    """Manager con dos books sanos y un desync recién ocurrido en 'ROTO' (seq=3)."""
    # seam_grace_sec=0: estos tests ejercitan el path de DESYNC REAL (cuarentena +
    # recovery) — la gracia del empalme (2026-08-05) clampearía el underflow y taparía
    # la maquinaria que se está probando. El clamp tiene su propia suite:
    # test_v2_empalme_siembra.py.
    manager = OrderbookManagerV2(mock_ws, seam_grace_sec=0.0)
    await manager.handle_message(_snapshot("SANO", seq=1))
    await manager.handle_message(_snapshot("ROTO", seq=2))
    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("ROTO", seq=3, delta="-500.00"))
    return manager


async def test_desync_consume_la_seq(mock_ws):
    """MECANISMO: el mensaje que desincroniza llegó EN SECUENCIA — el baseline avanza
    igual (lo roto es el book, no el stream)."""
    manager = await _manager_con_desync(mock_ws)

    assert manager._last_seq_by_sid[1] == 3
    assert manager._books["ROTO"].is_stale  # cuarentena intacta
    assert manager.get_top_of_book("ROTO", "yes") is None  # no sirve precios


async def test_el_mensaje_siguiente_no_es_un_gap_falso(mock_ws):
    """EL DEFECTO: con el baseline congelado, el siguiente mensaje EN SECUENCIA
    disparaba un gap falso que ciega el sid entero. Ahora pasa limpio y el hermano
    sano sigue operable."""
    manager = await _manager_con_desync(mock_ws)
    manager._last_recovery_start_mono.clear()  # descarta el ruido del rate-limit
    supresiones_antes = manager._recoveries_suppressed
    gaps_antes = manager.stats()["gaps_last_60s"]

    await manager.handle_message(_delta("SANO", seq=4))  # EN secuencia (3+1)

    assert manager._recoveries_suppressed == supresiones_antes  # sin supresión nueva
    assert manager.stats()["gaps_last_60s"] == gaps_antes  # sin gap falso
    assert not manager._books["SANO"].is_stale  # el hermano NO se cegó
    assert manager.get_top_of_book("SANO", "yes") is not None


async def test_gap_real_tras_un_desync_se_sigue_detectando(mock_ws):
    """CONTROL: consumir la seq del desync no tapa un gap REAL posterior. (La recovery
    que el desync disparó se completa primero: con el sid en _recovering todo se
    bufferea y no hay detección de gaps, por diseño.)"""
    manager = await _manager_con_desync(mock_ws)
    rid = manager._pending_req_id_for_sid(1)
    for i, t in enumerate(("SANO", "ROTO")):
        msg = _snapshot(t, seq=10 + i)
        msg["id"] = rid
        await manager.handle_message(msg)
    assert 1 not in manager._recovering
    manager._last_recovery_start_mono.clear()

    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("SANO", seq=9999))

    assert manager.stats()["gaps_last_60s"] == 1
    assert manager._books["SANO"].is_stale  # gap real → mass-stale (semántica intacta)


async def test_desync_no_adelanta_el_baseline_de_otros_tickers(mock_ws):
    """CONTROL: el baseline es del SID (la seq es global del stream), así que el desync
    de un ticker lo avanza al seq del mensaje — ni más ni menos. Un salto de más
    tragaría un gap real del ticker siguiente."""
    manager = await _manager_con_desync(mock_ws)

    assert manager._last_seq_by_sid[1] == 3  # exactamente el seq del mensaje, no más
