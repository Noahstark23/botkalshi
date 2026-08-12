"""
Sizes en CENTI-contratos (2026-08-12) — la aritmética exacta que mata el off-by-one.

La medición (agente web, noche completa del proceso #223, 22:00→03:31): tras #223,
la clase catastrófica de desyncs (brecha >10) cayó 1.900 → 0, pero sobrevivió una
población de 714 con 88,8% brecha=1 — la firma EXACTA del drift de redondeo de
parse_size = int(round(float)) POR MENSAJE: dos adds de 0.50 contratos redondeaban
0+0 mientras el exchange acumulaba 1.00, y el removal exacto del 1.00 underfloweaba
por 1. Ejemplo real: KXMLB-26-BOS delta -6093 contra bucket 6092.

El fix: el fixed-point de Kalshi trae exactamente 2 decimales → ×100 es EXACTO.
El book interno opera en centi-contratos (cero redondeo por mensaje); la conversión
a contratos enteros vive SOLO en get_top_of_book (floor, conservador), que además
exige ≥1 contrato entero para el mejor nivel (un nivel de polvo no tapa al operable).
El detector de corrupción real queda intacto: qty<0 en aritmética exacta ES feed roto.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    parse_size_centi,
)


def _manager() -> OrderbookManagerV2:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    return OrderbookManagerV2(ws, seam_grace_sec=0.0)


def _snap(ticker: str, yes_levels: list, seq: int = 1) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": seq,
        "msg": {"market_ticker": ticker, "yes_dollars_fp": yes_levels, "no_dollars_fp": []},
    }


def _delta(ticker: str, seq: int, price: str, delta: str) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": seq,
        "msg": {"market_ticker": ticker, "price_dollars": price, "delta_fp": delta, "side": "yes"},
    }


def test_parse_size_centi_es_exacto_con_dos_decimales():
    assert parse_size_centi("100.00") == 10_000
    assert parse_size_centi("6092.50") == 609_250
    assert parse_size_centi("0.50") == 50
    assert parse_size_centi("-15.00") == -1_500
    assert parse_size_centi(7) == 700  # int (shape viejo) = contratos enteros
    assert parse_size_centi(None) is None
    assert parse_size_centi("basura") is None


async def test_off_by_one_de_produccion_ya_no_desinca():
    """EL CASO 88.8%: dos adds de 0.50 (que el redondeo viejo hacía 0+0) y el removal
    del 1.00 exacto. Antes: 0 − 1 = −1 → desync → recovery sid-wide. Ahora: 50+50−100=0."""
    manager = _manager()
    await manager.handle_message(_snap("TICK", [["0.40", "10.00"]]))
    await manager.handle_message(_delta("TICK", 2, "0.35", "0.50"))
    await manager.handle_message(_delta("TICK", 3, "0.35", "0.50"))

    await manager.handle_message(_delta("TICK", 4, "0.35", "-1.00"))  # exacto: queda 0

    assert not manager._books["TICK"].is_stale
    assert 1 not in manager._recovering


async def test_corrupcion_real_sigue_desincando():
    """CONTROL: en aritmética exacta, un removal que excede lo acumulado ES corrupción
    — el detector no se debilitó, se volvió preciso."""
    manager = _manager()
    await manager.handle_message(_snap("TICK", [["0.40", "10.00"]]))

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", 2, "0.40", "-10.01"))


async def test_top_of_book_sirve_contratos_enteros():
    """La frontera de conversión: 100.00 entra como 10000 centi y sale como 100
    contratos — ningún consumidor (M1/M8/M9) ve la escala interna."""
    manager = _manager()
    await manager.handle_message(_snap("TICK", [["0.40", "100.00"]]))

    top = manager.get_top_of_book("TICK", "yes")
    assert top.best_bid.price_cents == 40 and top.best_bid.size == 100


async def test_nivel_de_polvo_no_tapa_al_operable():
    """Un nivel de 0.40 contratos (antes FILTRADO por el redondeo a 0, ahora presente
    como 40 centi) no puede ser el best bid: M1 no puede ejecutar contra polvo. El
    mejor nivel operable (≥1 contrato entero) es el que se sirve."""
    manager = _manager()
    await manager.handle_message(_snap("TICK", [["0.42", "0.40"], ["0.35", "5.00"]]))

    top = manager.get_top_of_book("TICK", "yes")
    assert top.best_bid.price_cents == 35  # el polvo de 42¢ no tapa
    assert top.best_bid.size == 5


async def test_profundidad_fraccional_se_reporta_floor():
    """Conservador: 5.90 contratos reales se sirven como 5 — jamás reportar más
    profundidad de la ejecutable (la dirección segura para el sizing de M1)."""
    manager = _manager()
    await manager.handle_message(_snap("TICK", [["0.40", "5.90"]]))

    top = manager.get_top_of_book("TICK", "yes")
    assert top.best_bid.size == 5
