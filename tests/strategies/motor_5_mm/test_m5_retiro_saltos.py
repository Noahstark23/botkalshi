"""
Retiro por salto (blindaje del maker — primer dato del gate 2026-08-13).

La evidencia: los primeros 4 fills del gate fueron de UN juego en vivo (CINCWS) con
markout −18/−20¢ a T+5min y AMBOS lados perdiendo a la vez — un evento del juego
atravesó las quotes. Es el perfil de víctima documentado del maker lento.

El blindaje: mark saltó ≥ MOTOR_MM_JUMP_RETREAT_CENTS desde el tick anterior.

SEMÁNTICA F1-v2: shadow y live aplican la misma política. La quote previa puede llenar
durante el salto (un maker real tampoco cancela en 0ms), pero después ambos retiran y no
crean una quote post-salto. Sin esta paridad, el gate mediría órdenes que live no pondría
y el inventario/skew futuro seguiría una trayectoria imposible.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from src.storage.models import MMShadowFill, get_session
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.engine import Motor5Engine


class _ReadOnlyClient:
    def __init__(self):
        self.books: dict[str, dict] = {}

    async def get_orderbook(self, ticker: str) -> dict:
        book = self.books.get(ticker)
        if book is None:
            raise RuntimeError("book no disponible")
        return {"orderbook": book}


def _book(yes_bid: int, yes_ask: int) -> dict:
    return {"yes": [[yes_bid, 100]], "no": [[100 - yes_ask, 100]]}


def _engine(client, jump_retreat: float = 5.0) -> Motor5Engine:
    eng = Motor5Engine(
        max_tickers=2,
        half_spread_cents=3,
        quote_size_contracts=10,
        max_inventory_contracts=50,
        fair_ttl_sec=600.0,
        jump_retreat_cents=jump_retreat,
    )
    eng._client = client
    return eng


@pytest.mark.asyncio
async def test_shadow_mide_fill_previo_y_retira_igual_que_live():
    """El salto puede llenar la quote previa, pero no nace otra quote post-salto."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)  # mid 50
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()  # quote resting 47/53
    assert "T-A" in eng._live_quotes

    client.books["T-A"] = _book(30, 36)  # mid 33: salto 17¢, y el ask 36 cruza el bid 47
    await eng._tick()

    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
    assert len(fills) == 1
    assert fills[0].mark_jump_cents == 17.0  # el salto del tick del fill
    assert fills[0].quote_jump_cents == 0.0  # su quote nació en un tick CALMO
    assert "T-A" not in eng._live_quotes


@pytest.mark.asyncio
async def test_live_si_retira_la_quote():
    """CONTROL del otro modo: con executor (F2/F3) el blindaje retira de VERDAD — ahí
    hay plata real que proteger y el fill envenenado se evita, no se mide."""
    from unittest.mock import AsyncMock

    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    eng._executor = AsyncMock()  # modo live

    client.books["T-A"] = _book(30, 36)  # salto 17¢
    await eng._tick()

    assert "T-A" not in eng._live_quotes  # retirada de verdad
    eng._executor.retire_ticker.assert_awaited()


@pytest.mark.asyncio
async def test_shadow_no_crea_quote_post_salto_y_rearma_en_tick_calmo():
    """Regresión: una quote off-policy post-salto no puede entrar a la cohorte F1."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()  # quote nace en tick calmo

    client.books["T-A"] = _book(30, 50)  # salto 10¢ (mid 50→40), sin cruce
    await eng._tick()
    assert "T-A" not in eng._live_quotes
    assert "T-A" not in eng._quote_jump

    client.books["T-A"] = _book(39, 43)  # mid 41: tick calmo desde mid 40
    await eng._tick()
    assert "T-A" in eng._live_quotes
    assert eng._quote_jump["T-A"] == 1.0


@pytest.mark.asyncio
async def test_movimiento_chico_no_retira_y_etiqueta_igual():
    """CONTROL: un drift de 2¢ (bajo el umbral de 5) cotiza normal; si hubiera fill,
    la etiqueta registra el valor chico — la columna es medición, no juicio."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()

    client.books["T-A"] = _book(42, 62)  # mid 52: salto 2¢, sin cruce
    await eng._tick()

    assert "T-A" in eng._live_quotes  # sigue cotizando


@pytest.mark.asyncio
async def test_retreat_cero_desactiva_el_blindaje():
    """CONTROL de config: 0 = off — comportamiento pre-blindaje (cotiza a través del
    salto). La etiqueta del fill se graba igual."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client, jump_retreat=0.0)
    await eng._tick()

    client.books["T-A"] = _book(30, 36)  # salto 17 con retreat off
    await eng._tick()

    assert "T-A" in eng._live_quotes  # re-cotizó igual (off)
    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
    assert fills[0].mark_jump_cents == 17.0  # la medición no se apaga nunca
