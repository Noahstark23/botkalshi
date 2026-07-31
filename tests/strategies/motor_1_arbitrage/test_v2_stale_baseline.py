"""
Gaps FALSOS por baseline congelado sobre books stale (forense 2026-07-31, parte 2).

Tras cortar la espiral (#204), el agente web midió en régimen estacionario: 258
supresiones/min con solo ~7 desyncs/min de semilla real, y el bot completamente ciego
el 32.9% del tiempo (muestreo de 4.274 lecturas). La causa NO era el feed de Kalshi:
un delta DROPEADO por book stale devolvía applied=False y el baseline del sid quedaba
congelado — el SIGUIENTE mensaje (que llegó perfectamente en secuencia) se comparaba
contra el baseline viejo y registraba un "gap" falso, cuya supresión marcaba stale los
217 books del sid → más drops → más "gaps". UNA cuarentena de UN ticker cegaba el sid
entero hasta la próxima recovery (~12s), en loop.

El fix: el drop por stale CONSUME la seq (la continuidad es del STREAM, no del book;
el book lo re-basea la recovery ya pedida). El buffer de bootstrap conserva su
semántica opuesta (baseline congelado hasta el snapshot inicial — pineada en
test_orderbook_manager_v2).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

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


async def test_delta_sobre_book_stale_consume_la_seq(mock_ws):
    """El drop por stale avanza el baseline del sid (el mensaje llegó en secuencia)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", seq=1))
    manager._books["TICK"].mark_stale()

    await manager.handle_message(_delta("TICK", seq=2))

    assert manager._last_seq_by_sid[1] == 2  # seq consumida
    assert manager._books["TICK"].is_stale  # el book sigue en cuarentena
    assert manager.get_top_of_book("TICK", "yes") is None  # y sigue sin servir precios
    assert mock_ws.send_command.await_count == 0  # y NO se pidió ninguna recovery


async def test_un_ticker_stale_no_ciega_a_sus_hermanos(mock_ws):
    """LA CASCADA DEL FORENSE: antes del fix, el primer delta del ticker stale congelaba
    el baseline y el delta EN SECUENCIA del hermano sano disparaba un gap falso →
    supresión → mark_stale del sid ENTERO. Ahora el hermano sano sigue operable."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("SANO", seq=1))
    await manager.handle_message(_snapshot("STALE", seq=2))
    manager._books["STALE"].mark_stale()  # semilla: una cuarentena puntual (desync)

    # Stream perfectamente en secuencia, intercalando ambos tickers.
    await manager.handle_message(_delta("STALE", seq=3))
    await manager.handle_message(_delta("SANO", seq=4))
    await manager.handle_message(_delta("STALE", seq=5))
    await manager.handle_message(_delta("SANO", seq=6))

    assert manager._recoveries_suppressed == 0  # cero gaps falsos
    assert 1 not in manager._recovering
    assert mock_ws.send_command.await_count == 0  # cero recoveries disparadas
    assert not manager._books["SANO"].is_stale  # el hermano NUNCA se cegó
    assert manager.get_top_of_book("SANO", "yes") is not None
    assert manager._last_seq_by_sid[1] == 6


async def test_gap_real_se_sigue_detectando_con_book_stale(mock_ws):
    """CONTROL: el fix no tapa gaps REALES — un salto de seq con el book stale sigue
    disparando la recovery (la continuidad del stream sí se rompió)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", seq=1))
    manager._books["TICK"].mark_stale()
    await manager.handle_message(_delta("TICK", seq=2))  # drop stale, seq consumida

    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("TICK", seq=99))  # salto REAL

    assert 1 in manager._recovering
    assert mock_ws.send_command.await_count == 1


async def test_bootstrap_sigue_congelando_el_baseline(mock_ws):
    """CONTROL: el sub-caso bootstrap conserva su semántica opuesta — un delta encolado
    (ticker sin snapshot inicial) NO consume la seq (el snapshot inicial no debe leerse
    como gap)."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("TICK", seq=1))

    await manager.handle_message(_delta("NUEVO", seq=2))  # sin snapshot inicial → buffer

    assert manager._last_seq_by_sid[1] == 1  # baseline congelado (semántica intacta)
    assert len(manager._bootstrap_buffer["NUEVO"]) == 1
