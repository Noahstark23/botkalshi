"""
Fair-at-fill del Motor 5 (2026-08-07) — la instrumentación que faltaba para el A/B.

El gap: mm_shadow_fills guardaba precio/count/rule pero NO el fair del momento del
fill. Con eso la pregunta central del gate F1→F2 —¿el fill capturó spread o fue
selección adversa (el fair se movió y el mercado nos cruzó)?— era incontestable:
66 fills históricos sin el dato. Ahora cada fill shadow persiste el fair de la quote
(tick t−1), el fair vigente al detectar el cruce (tick t) y el top-of-book del fill.
El dato se guarda CRUDO; el juicio (edge/drift) lo hace el análisis, no el hot path.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

import src.storage.models as models
from src.storage.models import MMShadowFill, get_session
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.engine import Motor5Engine


class _ReadOnlyClient:
    """Solo lectura de orderbook — la garantía de cero órdenes es estructural."""

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
async def test_fill_persiste_fair_de_quote_fair_vigente_y_book():
    """MECANISMO: el cruce del tick t contra la quote del tick t−1 registra AMBOS fairs
    y el top-of-book del momento del fill."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()  # quote resting: bid 47 / ask 53 sobre fair 0.50

    client.books["T-A"] = _book(40, 46)  # el ask cruza por debajo de nuestro bid
    FairValueBook.publish({"T-A": 0.44})  # y el fair YA se movió: selección adversa
    await eng._tick()

    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
    assert len(fills) == 1
    f = fills[0]
    assert f.side == "buy" and f.price_cents == 47
    assert f.quote_fair_prob == 0.50  # el fair con el que se cotizó
    assert f.fill_fair_prob == 0.44  # el fair cuando nos cruzaron
    assert f.yes_bid == 40 and f.yes_ask == 46  # el book del fill
    # El análisis puede ahora separar la pregunta: compramos a 47 con fair vigente 44
    # → edge −3c contra fair = selección adversa, no spread capturado.


@pytest.mark.asyncio
async def test_fill_sin_movimiento_de_fair_registra_el_mismo_fair():
    """CONTROL: cruce con fair quieto → quote_fair == fill_fair (spread capturado puro)."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()

    client.books["T-A"] = _book(54, 60)  # el bid cruza por encima de nuestro ask (53)
    FairValueBook.publish({"T-A": 0.50})  # fair no se movió
    await eng._tick()

    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
    assert len(fills) == 1
    f = fills[0]
    assert f.side == "sell" and f.price_cents == 53
    assert f.quote_fair_prob == f.fill_fair_prob == 0.50


def test_migracion_agrega_columnas_a_tabla_existente(tmp_path):
    """La DB de producción tiene mm_shadow_fills SIN estas columnas: apply_migrations
    debe agregarlas (idempotente) y las filas viejas quedan NULL, no rotas."""
    from sqlalchemy import create_engine

    db = tmp_path / "vieja.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        # El schema histórico de la tabla, pre fair-at-fill.
        conn.exec_driver_sql(
            "CREATE TABLE mm_shadow_fills ("
            "id INTEGER PRIMARY KEY, ticker VARCHAR(100), side VARCHAR(4), "
            "price_cents INTEGER, count INTEGER, fee_cents INTEGER, "
            "rule VARCHAR(50), inventory_after INTEGER, created_at DATETIME)"
        )
        conn.exec_driver_sql(
            "INSERT INTO mm_shadow_fills "
            "(ticker, side, price_cents, count, fee_cents, rule, inventory_after) "
            "VALUES ('T-VIEJA', 'buy', 47, 10, 1, 'ask 46 < bid 47', 10)"
        )

    from unittest.mock import MagicMock, patch

    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    with patch("src.storage.models.get_settings", return_value=settings):
        models.apply_migrations(engine)
        models.apply_migrations(engine)  # idempotente: la segunda pasada no revienta

    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(mm_shadow_fills)")}
        assert {"quote_fair_prob", "fill_fair_prob", "yes_bid", "yes_ask"} <= cols
        indexes = {r[1] for r in conn.exec_driver_sql("PRAGMA index_list(mm_shadow_fills)")}
        assert "ix_mm_shadow_fills_experiment_metric_created" in indexes
        row = conn.exec_driver_sql(
            "SELECT quote_fair_prob, fill_fair_prob, yes_bid, yes_ask "
            "FROM mm_shadow_fills WHERE ticker='T-VIEJA'"
        ).fetchone()
        assert row == (None, None, None, None)  # la fila vieja queda NULL y legible
