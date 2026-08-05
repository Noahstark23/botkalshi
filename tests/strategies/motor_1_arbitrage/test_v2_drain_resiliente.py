"""
Drains resilientes al desync (2026-08-05) — el generador dominante de desyncs.

Forense n=10.712: el 100% de los desyncs son deltas NEGATIVOS sobre niveles PRESENTES
con cantidad insuficiente, concentrados en los tickers de más tráfico. La hipótesis del
redondeo quedó relegada (bucket_qty_pre_delta=0 en 0 casos; déficits de hasta −35.944
que ninguna deriva de ±0.5 produce). El mecanismo verificado en código: los dos drains
(`_drain_buffer` y `_drain_bootstrap_buffer`) popean su buffer y NO tenían try/except
por mensaje — un desync a mitad del drain abortaba el resto y perdía en silencio los
deltas encolados de TODOS los tickers, dejando books cortos/inflados SIN marcar stale.
Perder una suma = book corto = el próximo delta negativo explota → recovery → drain →
otro desync → más pérdida. Autoalimentado.

Fix (Lección 7): cuarentenar el ticker que desyncó y SEGUIR drenando. El contador
`drain_desyncs_total` mide cuánto del generador vivía acá — criterio falsable: si la
tasa de ~500/h cae fuerte, este era el motor; si no cae, el generador es upstream.
"""

from __future__ import annotations

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


async def test_desync_en_drain_de_recovery_cuarentena_y_sigue(mock_ws):
    """El caso con deltas POSTERIORES al snapshot: el desync de B no aborta el drain —
    A recibe su delta, B queda en cuarentena, contador sube."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("A", seq=1))
    await manager.handle_message(_snapshot("B", seq=2))
    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("A", seq=10))
    # Encolados con seq > snapshot de recovery (20/21) → el drain los aplica:
    await manager.handle_message(_delta("B", seq=30, delta="-500.00"))  # DESYNC al drenar
    await manager.handle_message(_delta("A", seq=31, delta="7.00"))  # debe APLICAR igual

    rid = manager._pending_req_id_for_sid(1)
    await manager.handle_message(_snapshot("A", sid=1, seq=20, req_id=rid))
    await manager.handle_message(_snapshot("B", sid=1, seq=21, req_id=rid))

    assert manager.stats()["drain_desyncs_total"] == 1
    assert manager._books["B"].is_stale  # el roto, en cuarentena
    top = manager.get_top_of_book("A", "yes")
    assert top is not None and top.best_bid.size == 107  # 100 del snapshot + 7 del drain
    assert manager._last_seq_by_sid[1] == 31  # baseline al día (el drain siguió)


async def test_desync_en_drain_de_bootstrap_no_rompe_la_recovery(mock_ws):
    """El drain de bootstrap corre DENTRO de _apply_snapshot_msg: un desync ahí abortaba
    además la contabilidad de la recovery (timeout del watchdog). Ahora: cuarentena y
    la recovery completa normal."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_delta("NUEVO", seq=5, delta="-500.00"))  # → bootstrap buffer
    await manager.handle_message(_delta("NUEVO", seq=6, delta="3.00"))  # → bootstrap buffer

    await manager.handle_message(_snapshot("NUEVO", seq=4))  # snapshot inicial → drena

    # El primer delta (seq 5 > 4) desyncó al drenar → cuarentena; el drain siguió.
    assert manager.stats()["drain_desyncs_total"] == 1
    assert manager._books["NUEVO"].is_stale


async def test_contador_de_deltas_fraccionales(mock_ws):
    """Tasa base pedida por el forense: fractional sube si y solo si el delta trae
    parte decimal ≠ 0. Sin esta base, el 14.76% de fraccionales en los desyncs no se
    puede testear por enriquecimiento."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("A", seq=1))

    await manager.handle_message(_delta("A", seq=2, delta="10.00"))  # entero
    await manager.handle_message(_delta("A", seq=3, delta="10.50"))  # fraccional
    await manager.handle_message(_delta("A", seq=4, delta="-0.47"))  # fraccional

    s = manager.stats()
    assert s["deltas_total"] == 3
    assert s["deltas_fractional_total"] == 2
