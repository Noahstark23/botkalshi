"""
Empalme de siembra (2026-08-05) — la semilla que quedaba del generador de desyncs.

Forense n=4.752 post-#211: el 63.8% de los desyncs ocurre ≤5s después de sembrar el
book, 96.3% en ≤60s, y UNO solo en un book de más de 5 minutos. El contenido del
snapshot de Kalshi llega más viejo que su sello de seq: el drain descarta los deltas
≤ sello (correcto según el sello), pero los INCREMENTOS de esa ventanita no están en
el contenido → book corto → el primer removal grande underflowea segundos después.

Fix en la dirección FAIL-SAFE: dentro de la gracia post-snapshot, el underflow se
CLAMPEA a 0 en vez de desincronizar. Clampear solo puede SUBESTIMAR el book (la qty
real es ≥ 0 = la clampeada) — jamás crea liquidez fantasma; el fantasma nace de
perder REMOVALS, que no pasan por este camino. Fuera de la gracia, el path de desync
queda INTACTO (cuarentena + recovery): la línea ERROR de diagnóstico pasa a medir
solo los desyncs post-gracia, la señal limpia.

Criterio falsable del forense: el balde ≤5s (63.8% del volumen) tiene que colapsar —
la tasa de ~216/h debe caer muy por debajo de 80/h. Si queda >150/h, el balde era el
lazo desync→recovery y no la causa.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


def _snapshot(ticker: str, sid: int = 1, seq: int = 1) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.50", "100.00"], ["0.40", "30.00"]],
            "no_dollars_fp": [],
        },
    }


def _delta(ticker: str, seq: int, sid: int = 1, delta: str = "10.00", price: str = "0.50") -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": price,
            "delta_fp": delta,
            "side": "yes",
        },
    }


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    return ws


async def test_underflow_dentro_de_la_gracia_clampea_sin_desincronizar(mock_ws):
    """EL CASO DEL FORENSE: removal de −500 sobre 100 a segundos de la siembra — antes
    desync + cuarentena + recovery; ahora el nivel clampea a 0 y el book SIGUE VIVO."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", seq=1))

    await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))  # no raise

    assert manager.stats()["seam_clamps_total"] == 1
    book = manager._books["TICK"]
    assert not book.is_stale  # sin cuarentena
    assert book.sequence == 2  # la seq avanzó (el stream sigue coherente)
    top = manager.get_top_of_book("TICK", "yes")
    assert top is not None and top.best_bid.price_cents == 40  # el nivel 50¢ quedó en 0
    assert mock_ws.send_command.await_count == 0  # sin recovery disparada
    assert manager._recoveries_suppressed == 0


async def test_el_clamp_jamas_infla_el_book(mock_ws):
    """FAIL-SAFE: clampear solo achica. El nivel clampeado desaparece (0), no queda
    liquidez inventada — un book subestimado hace que M1 opere de menos, nunca de más."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", seq=1))
    await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))

    view = manager._books["TICK"].snapshot_view()
    assert 50 not in view["yes_bids"]  # nivel removido, no negativo ni fantasma
    # snapshot_view expone el estado INTERNO: centi-contratos (30.00 = 3000).
    assert view["yes_bids"].get(40) == 3000  # los demás niveles intactos


async def test_fuera_de_la_gracia_el_desync_sigue_intacto(mock_ws):
    """CONTROL: pasada la gracia, el underflow es un desync REAL (book viejo divergido)
    y conserva el path completo — cuarentena + recovery. La semántica de #204-#211 no
    se toca; solo se filtra el ruido del empalme."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", seq=1))
    manager._seeded_at_mono["TICK"] = time.monotonic() - 99.0  # gracia vencida

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))

    assert manager.stats()["seam_clamps_total"] == 0
    assert manager._books["TICK"].is_stale  # cuarentena como siempre
    assert 1 in manager._recovering  # recovery disparada


async def test_gracia_cero_es_el_comportamiento_pre_fix(mock_ws):
    """CONTROL de config: ORDERBOOK_V2_SEAM_GRACE_SEC=0 desactiva el clamp."""
    manager = OrderbookManagerV2(mock_ws, seam_grace_sec=0.0)
    await manager.handle_message(_snapshot("TICK", seq=1))

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))

    assert manager.stats()["seam_clamps_total"] == 0


async def test_deltas_normales_en_gracia_aplican_normal(mock_ws):
    """CONTROL: la gracia no altera los deltas sanos — sumas y restas válidas aplican
    idéntico dentro y fuera de la ventana."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", seq=1))

    await manager.handle_message(_delta("TICK", seq=2, delta="-40.00"))  # 100−40=60, válido
    await manager.handle_message(_delta("TICK", seq=3, delta="15.00"))  # 60+15=75

    assert manager.stats()["seam_clamps_total"] == 0
    top = manager.get_top_of_book("TICK", "yes")
    assert top.best_bid.size == 75
