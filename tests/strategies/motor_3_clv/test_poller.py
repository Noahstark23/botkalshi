"""
Tests del PortfolioPoller (Motor 3) — el bug que lo dejaba ciego: Kalshi devuelve la
cantidad de la posición en `position_fp` (fixed-point string, ej. "-1.00"), NO en
`position`. Antes el poller leía `position` → None → descartaba TODA posición como
"cantidad cero" → portfolio_positions quedaba vacía → Motor 3 escaneaba 0 posiciones.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import src.storage.models as models
from src.storage.models import PortfolioPosition
from src.strategies.motor_3_clv.poller import PortfolioPoller


def _client(positions: list[dict]) -> AsyncMock:
    """AsyncMock que actúa como KalshiRestClient (async context manager)."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get_positions = AsyncMock(return_value={"market_positions": positions, "cursor": None})
    client.get_market = AsyncMock(return_value={"market": {"close_time": "2026-06-26T20:00:00Z"}})
    return client


def _positions() -> list[PortfolioPosition]:
    with models.get_session() as s:
        return list(s.exec(select(PortfolioPosition)))


@pytest.mark.asyncio
async def test_parses_position_fp_and_persists(monkeypatch):
    """position_fp='-1.00' → se parsea a -1 (side=no, count=1) y se persiste."""
    monkeypatch.setattr("src.strategies.motor_3_clv.poller.asyncio.sleep", AsyncMock())
    client = _client(
        [
            {
                "ticker": "KXWCGAME-26JUN26NZLBEL-BEL",
                "position_fp": "-1.00",
                "market_exposure_dollars": "0.22",  # dinero = string USD → 22¢
            }
        ]
    )
    poller = PortfolioPoller(client_factory=lambda: client)

    n = await poller.sync_once()

    assert n == 1  # ANTES era 0 (el bug): descartaba la posición real
    rows = _positions()
    assert len(rows) == 1
    assert rows[0].ticker == "KXWCGAME-26JUN26NZLBEL-BEL"
    assert rows[0].side == "no" and rows[0].count == 1
    assert rows[0].exposure_cents == 22  # market_exposure_dollars "0.22" → 22¢
    assert rows[0].close_time is not None  # resolvió close_time vía get_market


@pytest.mark.asyncio
async def test_zero_position_fp_skipped(monkeypatch):
    """position_fp='0.00' → posición cerrada, no se persiste."""
    monkeypatch.setattr("src.strategies.motor_3_clv.poller.asyncio.sleep", AsyncMock())
    client = _client([{"ticker": "T-ZERO", "position_fp": "0.00"}])
    poller = PortfolioPoller(client_factory=lambda: client)

    n = await poller.sync_once()

    assert n == 0
    assert _positions() == []


@pytest.mark.asyncio
async def test_plain_position_fallback(monkeypatch):
    """Fallback: si la API devolviera `position` plano (int), igual se parsea."""
    monkeypatch.setattr("src.strategies.motor_3_clv.poller.asyncio.sleep", AsyncMock())
    client = _client([{"ticker": "T-PLAIN", "position": 3, "market_exposure": 150}])
    poller = PortfolioPoller(client_factory=lambda: client)

    n = await poller.sync_once()

    assert n == 1
    rows = _positions()
    assert rows[0].side == "yes" and rows[0].count == 3
    assert rows[0].exposure_cents == 150  # market_exposure plano (centavos) → 150


@pytest.mark.asyncio
async def test_exposure_none_when_field_absent_persists_position(monkeypatch):
    """Sin ningún campo de exposición conocido → exposure_cents None, pero la posición
    igual se persiste (el bug que arregla esto era cosmético: no debe tirar el poller)."""
    monkeypatch.setattr("src.strategies.motor_3_clv.poller.asyncio.sleep", AsyncMock())
    client = _client([{"ticker": "T-NOEXP", "position_fp": "2.00"}])  # sin market_exposure*
    poller = PortfolioPoller(client_factory=lambda: client)

    n = await poller.sync_once()

    assert n == 1
    rows = _positions()
    assert rows[0].side == "yes" and rows[0].count == 2
    assert rows[0].exposure_cents is None  # ausente → None, sin crash


# =====================================================
# Fix auditoría 2026-07-01: peak_bid_cents no sobrevive a un flip de side
# =====================================================


def test_upsert_resets_peak_on_side_flip():
    """La posición flipea de side entre polls (yes→no sin pasar por 0 en un snapshot): el
    peak del episodio ANTERIOR no puede quedar pegado a la posición NUEVA — un peak=80 del
    lado yes contra una posición no con entry 25 disparaba el trailing al primer tick
    (venta espuria, potencialmente en pérdida)."""
    with models.get_session() as s:
        s.add(models.PortfolioPosition(ticker="KXFLIP", side="yes", count=5, peak_bid_cents=80))
        s.commit()

    poller = PortfolioPoller()
    poller._persist({"KXFLIP": ("no", 3, None)}, {"KXFLIP": None})

    with models.get_session() as s:
        row = s.exec(
            select(models.PortfolioPosition).where(models.PortfolioPosition.ticker == "KXFLIP")
        ).first()
    assert row.side == "no" and row.count == 3
    assert row.peak_bid_cents is None  # peak reseteado: posición de identidad nueva


def test_upsert_preserves_peak_same_side():
    """Update normal (mismo side): el peak del trailing se PRESERVA entre syncs."""
    with models.get_session() as s:
        s.add(models.PortfolioPosition(ticker="KXKEEP", side="yes", count=5, peak_bid_cents=77))
        s.commit()

    poller = PortfolioPoller()
    poller._persist({"KXKEEP": ("yes", 8, None)}, {"KXKEEP": None})

    with models.get_session() as s:
        row = s.exec(
            select(models.PortfolioPosition).where(models.PortfolioPosition.ticker == "KXKEEP")
        ).first()
    assert row.count == 8 and row.peak_bid_cents == 77


# =====================================================
# Deudas auditoría 2026-07-01: robustez del poller
# =====================================================


@pytest.mark.asyncio
async def test_purge_deferred_on_first_empty_result(monkeypatch):
    """Respuesta vacía con filas existentes → NO purga al primer sync (puede ser
    shape-drift '200 OK'); purga recién con el segundo vacío consecutivo."""
    monkeypatch.setattr("src.strategies.motor_3_clv.poller.asyncio.sleep", AsyncMock())
    with models.get_session() as s:
        s.add(models.PortfolioPosition(ticker="KXG", side="yes", count=5, peak_bid_cents=88))
        s.commit()

    client = _client([])  # respuesta vacía
    poller = PortfolioPoller(client_factory=lambda: client)

    await poller.sync_once()
    rows = _positions()
    assert len(rows) == 1 and rows[0].peak_bid_cents == 88  # el peak sobrevive al blip

    await poller.sync_once()  # segundo vacío consecutivo → confirmado, purga
    assert _positions() == []


@pytest.mark.asyncio
async def test_close_time_refreshed_near_close(monkeypatch):
    """close_time cacheado DENTRO de la ventana de refresh (45 min) → se re-consulta
    get_market (prórroga/early close mueven el close real y con él la ventana T-30)."""
    monkeypatch.setattr("src.strategies.motor_3_clv.poller.asyncio.sleep", AsyncMock())
    from src.strategies.motor_3_clv.poller import _naive_utc_now

    near_close = _naive_utc_now() + timedelta(minutes=30)
    with models.get_session() as s:
        s.add(models.PortfolioPosition(ticker="KXNEAR", side="yes", count=5, close_time=near_close))
        s.commit()

    moved = (_naive_utc_now() + timedelta(minutes=90)).replace(microsecond=0)
    client = _client([{"ticker": "KXNEAR", "position_fp": "5.00"}])
    client.get_market = AsyncMock(return_value={"market": {"close_time": moved.isoformat() + "Z"}})
    poller = PortfolioPoller(client_factory=lambda: client)

    await poller.sync_once()

    client.get_market.assert_awaited()  # cache invalidado por cercanía al cierre
    rows = _positions()
    assert rows[0].close_time == moved  # prórroga aplicada


@pytest.mark.asyncio
async def test_close_time_cached_when_far_from_close(monkeypatch):
    """Lejos del cierre el cache vale: NO se re-consulta get_market."""
    monkeypatch.setattr("src.strategies.motor_3_clv.poller.asyncio.sleep", AsyncMock())
    from src.strategies.motor_3_clv.poller import _naive_utc_now

    far_close = _naive_utc_now() + timedelta(hours=5)
    with models.get_session() as s:
        s.add(models.PortfolioPosition(ticker="KXFAR", side="yes", count=5, close_time=far_close))
        s.commit()

    client = _client([{"ticker": "KXFAR", "position_fp": "5.00"}])
    poller = PortfolioPoller(client_factory=lambda: client)

    await poller.sync_once()
    client.get_market.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_cursor_stops_pagination(monkeypatch):
    """Cursor pegado (mismo valor en cada página) → la paginación corta en vez de
    colgar el tick para siempre."""
    monkeypatch.setattr("src.strategies.motor_3_clv.poller.asyncio.sleep", AsyncMock())
    client = _client([])
    client.get_positions = AsyncMock(return_value={"market_positions": [], "cursor": "STUCK"})
    poller = PortfolioPoller(client_factory=lambda: client)

    await poller.sync_once()  # sin guard, esto no retornaba nunca
    assert client.get_positions.await_count <= 3


def test_money_to_cents_coalesces_none_present():
    """Key plana PRESENTE con None ya no enmascara el fallback `_cents`."""
    from src.strategies.motor_3_clv.poller import _money_to_cents

    assert (
        _money_to_cents({"market_exposure": None, "market_exposure_cents": 150}, "market_exposure")
        == 150
    )
