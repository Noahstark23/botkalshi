"""
Colisión de precios SUB-CÉNTIMO en apply_snapshot (forense 2026-08-11) — la fábrica
raíz de los desyncs crónicos de los season markets.

La evidencia (auditoría en vivo, 21 min de log): 55 desyncs (uno cada ~23s), SIEMPRE
el mismo patrón — delta a precio 0.9950 con delta_fp=-2000 produce qty=-1964 < 0 en
KXMLB-26-TOR / KXMLB-26-NYM (futuros de campeón: longshots con NO bid a 99.5¢).
Cada desync dispara recovery sid-wide de 313 tickers → re-siembra → embargo del guard
de M1 re-armado → 1032/1054 señales bloqueadas. total_completadas=7913 recoveries.

La aritmética que lo delata: el mapeo int(round(float*100)) colapsa niveles
sub-céntimo DISTINTOS del exchange en el mismo bucket entero:
    0.9950 → 99.5 → bucket 100
    0.9960 → 99.6 → bucket 100
    0.9990 → 99.9 → bucket 100
y apply_snapshot construía el dict con SOBREESCRITURA (dict[price] = size): un
snapshot con [["0.9950","2000"], ["0.9960","36"]] dejaba bucket 100 = 36 (pisó los
2000). El removal posterior de -2000 @0.9950 → 36 − 2000 = −1964. EXACTO el número
del log.

El fix: SUMAR en colisión. Con suma consistente en snapshot (los deltas ya suman por
construcción: qty = current + delta), un bucket jamás puede ir negativo por mensajes
en secuencia — el removal de cualquier nivel del exchange cae en el bucket donde
cayeron sus adds. El merge de niveles sub-céntimo en un bucket entero es deliberado
y seguro: M1 opera en centavos enteros y el top-of-book agregado es correcto.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError, OrderbookState
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


def _manager() -> OrderbookManagerV2:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    # seam_grace=0: los desyncs de producción ocurren FUERA de la gracia del empalme
    # (el clamp taparía el path que este test ejercita).
    return OrderbookManagerV2(ws, seam_grace_sec=0.0)


# =====================================================
# Nivel state: la colisión suma, no sobreescribe
# =====================================================


def test_snapshot_con_colision_suma_los_niveles():
    state = OrderbookState("KXMLB-26-TOR")
    # 0.9950 y 0.9960 ya llegan parseados como bucket 100 (parse_price_to_cents).
    state.apply_snapshot({"seq": 1, "yes": [[100, 2000], [100, 36]], "no": []})

    top = state.top_of_book("yes")
    assert top.best_bid.price_cents == 100
    assert top.best_bid.size == 2036  # 2000 + 36: nada se pisa


def test_removal_del_nivel_colisionado_no_desinca():
    """EL CASO DE PRODUCCIÓN: qty=-1964 era 36 − 2000 tras el overwrite. Con suma,
    el removal de -2000 deja los 36 del otro nivel sub-céntimo. Sin desync."""
    state = OrderbookState("KXMLB-26-TOR")
    state.apply_snapshot({"seq": 1, "yes": [[100, 2000], [100, 36]], "no": []})

    state.apply_delta({"side": "yes", "price": 100, "delta": -2000, "seq": 2})

    assert state.top_of_book("yes").best_bid.size == 36


# =====================================================
# Wire-format por el manager: el payload EXACTO del log de producción
# =====================================================


async def test_payload_de_produccion_no_desinca_mas():
    """El mensaje que generaba un desync cada ~23s, tal cual viaja por el WS:
    snapshot fixed-point con dos niveles sub-céntimo del mismo bucket, y el delta
    de removal que producía qty=-1964 → cuarentena → recovery de 313 tickers."""
    manager = _manager()
    await manager.handle_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "KXMLB-26-TOR",
                "yes_dollars_fp": [["0.9950", "2000.00"], ["0.9960", "36.00"]],
                "no_dollars_fp": [],
            },
        }
    )

    # Antes del fix esto levantaba OrderbookDesyncError (36 − 2000 = −1964).
    await manager.handle_message(
        {
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 2,
            "msg": {
                "market_ticker": "KXMLB-26-TOR",
                "price_dollars": "0.9950",
                "delta_fp": "-2000.00",
                "side": "yes",
            },
        }
    )

    top = manager.get_top_of_book("KXMLB-26-TOR", "yes")
    assert top is not None and top.best_bid.size == 36
    assert not manager._books["KXMLB-26-TOR"].is_stale  # sin cuarentena
    assert 1 not in manager._recovering  # sin recovery sid-wide disparada


async def test_removal_mayor_que_el_bucket_sigue_desincando():
    """CONTROL: la corrupción REAL del feed no se tapa — un removal que excede el
    bucket completo (todos sus niveles sub-céntimo sumados) sigue siendo desync,
    cuarentena y recovery. El fix elimina el artefacto, no el detector."""
    manager = _manager()
    await manager.handle_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {
                "market_ticker": "KXMLB-26-TOR",
                "yes_dollars_fp": [["0.9950", "2000.00"], ["0.9960", "36.00"]],
                "no_dollars_fp": [],
            },
        }
    )

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 2,
                "msg": {
                    "market_ticker": "KXMLB-26-TOR",
                    "price_dollars": "0.9950",
                    "delta_fp": "-2100.00",
                    "side": "yes",
                },
            }
        )
