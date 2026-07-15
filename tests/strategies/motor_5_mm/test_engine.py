"""Motor5Engine (F1 shadow): flujo del tick de punta a punta, sin una sola orden.

El 'cliente' fake solo tiene get_orderbook — si el engine intentara colocar/cancelar una
orden, el AttributeError haría fallar el test: la garantía de CERO órdenes es estructural.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from src.storage.models import MMFunnelSnapshot, MMQuote, MMShadowFill, get_session
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.engine import Motor5Engine


class _ReadOnlyClient:
    """Solo lectura de orderbook. Sin place_order/cancel_order a propósito."""

    def __init__(self):
        self.books: dict[str, dict] = {}

    async def get_orderbook(self, ticker: str) -> dict:
        book = self.books.get(ticker)
        if book is None:
            raise RuntimeError("book no disponible")
        return {"orderbook": book}


def _book(yes_bid: int | None, yes_ask: int | None) -> dict:
    yes = [[yes_bid, 100]] if yes_bid is not None else []
    no = [[100 - yes_ask, 100]] if yes_ask is not None else []
    return {"yes": yes, "no": no}


def _engine(client) -> Motor5Engine:
    eng = Motor5Engine(
        max_tickers=2,
        half_spread_cents=3,
        quote_size_contracts=10,
        max_inventory_contracts=50,
        fair_ttl_sec=600.0,
    )
    eng._client = client
    return eng


@pytest.mark.asyncio
async def test_tick_quotes_and_persists():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    with get_session() as s:
        quotes = list(s.exec(select(MMQuote)))
        snaps = list(s.exec(select(MMFunnelSnapshot)))
    assert len(quotes) == 1
    q = quotes[0]
    assert q.ticker == "T-A" and q.bid_cents == 47 and q.ask_cents == 53
    assert q.yes_bid == 40 and q.yes_ask == 60
    assert len(snaps) == 1 and snaps[0].quoted == 1 and snaps[0].fills == 0


@pytest.mark.asyncio
async def test_cross_on_next_tick_fills_and_updates_inventory():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()  # quote resting: bid 47 / ask 53
    client.books["T-A"] = _book(40, 46)  # el ask del book cruza POR DEBAJO de nuestro bid
    await eng._tick()
    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
    assert len(fills) == 1
    f = fills[0]
    assert f.side == "buy" and f.price_cents == 47 and f.count == 10
    assert f.inventory_after == 10
    assert eng._inventory.net("T-A") == 10


@pytest.mark.asyncio
async def test_touch_never_fills():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    client.books["T-A"] = _book(40, 47)  # TOCA el bid (==47), no cruza
    await eng._tick()
    with get_session() as s:
        assert list(s.exec(select(MMShadowFill))) == []


@pytest.mark.asyncio
async def test_no_book_skips_and_retires_live_quote():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    assert "T-A" in eng._live_quotes
    del client.books["T-A"]  # book caído
    await eng._tick()
    assert "T-A" not in eng._live_quotes  # no se cotiza (ni se llena) a ciegas
    with get_session() as s:
        snaps = list(s.exec(select(MMFunnelSnapshot)))
    assert snaps[-1].skip_no_book == 1 and snaps[-1].quoted == 0


@pytest.mark.asyncio
async def test_stale_fair_shrinks_universe():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    eng._fair_ttl = 0.0  # todo fair es viejo
    await eng._tick()
    with get_session() as s:
        snaps = list(s.exec(select(MMFunnelSnapshot)))
        assert list(s.exec(select(MMQuote))) == []
    assert snaps[0].fair_fresh == 0 and snaps[0].quoted == 0


@pytest.mark.asyncio
async def test_max_tickers_caps_universe_deterministically():
    client = _ReadOnlyClient()
    for t in ("T-A", "T-B", "T-C"):
        client.books[t] = _book(40, 60)
    FairValueBook.publish({"T-C": 0.5, "T-A": 0.5, "T-B": 0.5})
    eng = _engine(client)  # max_tickers=2
    await eng._tick()
    with get_session() as s:
        quoted = {q.ticker for q in s.exec(select(MMQuote))}
    assert quoted == {"T-A", "T-B"}  # orden alfabético, capado


@pytest.mark.asyncio
async def test_ticker_leaving_universe_retires_quote():
    """El fair expira → la quote viva se retira: el book cruzando DESPUÉS no genera fill
    (una quote que ya no existe no se llena)."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    FairValueBook.clear()  # fair desaparece (equivale a TTL vencido)
    client.books["T-A"] = _book(40, 44)  # cruce que ANTES habría llenado
    await eng._tick()
    with get_session() as s:
        assert list(s.exec(select(MMShadowFill))) == []
    assert "T-A" not in eng._live_quotes


# =====================================================
# Shape 2026 del orderbook REST + diagnóstico one-shot (incidente 2026-07-14)
# =====================================================


@pytest.mark.asyncio
async def test_book_top_parses_fixed_point_shape():
    """Shape 2026 (*_dollars_fp): el parser NO debe leer vacío en silencio (la causa
    candidata de skip_no_book=~11k/día sin un solo error en logs)."""
    client = AsyncMock()
    client.get_orderbook.return_value = {
        "orderbook": {
            "yes_dollars_fp": [["0.4200", "50.00"]],
            "no_dollars_fp": [["0.5500", "30.00"]],
        }
    }
    eng = _engine(client)
    top = await eng._book_top("KXMLBGAME-X")
    assert top == (42, 45)  # yes_bid=42; yes_ask=100−55=45


@pytest.mark.asyncio
async def test_book_top_legacy_shape_still_works():
    """CONTROL: el shape legacy (yes/no en cents) sigue parseando igual que siempre."""
    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"yes": [[42, 50]], "no": [[55, 30]]}}
    eng = _engine(client)
    top = await eng._book_top("KXMLBGAME-X")
    assert top == (42, 45)


@pytest.mark.asyncio
async def test_unknown_shape_logs_diagnostic_once_per_ticker():
    """Book inusable → None (skip_no_book) con log one-shot POR TICKER: el mismo ticker
    no re-loguea, pero otro ticker con problema SÍ (un bool global dejaba que el primer
    ticker consumiera el diagnóstico de todos los demás)."""
    from loguru import logger as _logger

    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"claves_nuevas_2027": []}}
    eng = _engine(client)
    records: list[str] = []
    sink = _logger.add(records.append, level="WARNING", format="{message}")
    try:
        assert await eng._book_top("KXMLBGAME-X") is None
        assert await eng._book_top("KXMLBGAME-X") is None  # mismo ticker: sin re-log
        assert await eng._book_top("KXMLBGAME-Y") is None  # OTRO ticker: log propio
    finally:
        _logger.remove(sink)
    shape_logs = [r for r in records if "motor5.book_shape" in r]
    assert len(shape_logs) == 2
    assert "KXMLBGAME-X" in shape_logs[0] and "claves_nuevas_2027" in shape_logs[0]
    assert "KXMLBGAME-Y" in shape_logs[1]


@pytest.mark.asyncio
async def test_chronic_skip_relogs_after_backoff():
    """Un skip CRÓNICO re-loguea tras BOOK_DIAG_REARM_SEC: el one-shot de por vida se
    consumía (p.ej. en el boot, fuera de la ventana de log visible) y 127 funnels con
    skip_book>0 quedaban sin UNA línea de causa (forense 2026-07-15)."""
    from loguru import logger as _logger

    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"yes": [], "no": []}}
    eng = _engine(client)
    records: list[str] = []
    sink = _logger.add(records.append, level="WARNING", format="{message}")
    try:
        assert await eng._book_top("KXMLBGAME-X") is None  # log 1
        assert await eng._book_top("KXMLBGAME-X") is None  # dentro del backoff: silencio
        # Simular que pasó el backoff (envejecer el timestamp guardado, sin mockear reloj).
        eng._book_diag_last["KXMLBGAME-X"] -= eng.BOOK_DIAG_REARM_SEC
        assert await eng._book_top("KXMLBGAME-X") is None  # log 2 (re-armado)
    finally:
        _logger.remove(sink)
    assert len([r for r in records if "motor5.book_shape" in r]) == 2


@pytest.mark.asyncio
async def test_empty_book_diagnostic_distinguishes_thin_market():
    """CONTROL forense: claves correctas pero listas VACÍAS (book fino, sin resting) →
    el log dice yes_levels=0/no_levels=0 — distinguible de un problema de shape."""
    from loguru import logger as _logger

    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"yes": [], "no": []}}
    eng = _engine(client)
    records: list[str] = []
    sink = _logger.add(records.append, level="WARNING", format="{message}")
    try:
        assert await eng._book_top("KXMLBGAME-Z") is None
    finally:
        _logger.remove(sink)
    shape_logs = [r for r in records if "motor5.book_shape" in r]
    assert len(shape_logs) == 1
    assert "yes_levels=0" in shape_logs[0] and "no_levels=0" in shape_logs[0]
