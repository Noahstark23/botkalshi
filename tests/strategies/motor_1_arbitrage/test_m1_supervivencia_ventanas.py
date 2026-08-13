"""
Supervivencia de ventanas binarias (veredicto estructural M1, 2026-08-13).

La investigación del mecanismo: Kalshi tiene UN solo book — yes_ask+no_ask<100 ⇔
book auto-cruzado, y el MATCHING ENGINE consume el cruce al arribo de la orden
cruzante (mintea el par él mismo). Las ventanas de 30-140ms que M1 ve son deltas WS
de un cruce YA muerto: la carrera es de 0ms y por eso 0/46 capturas.

Esta telemetría lo entierra (o refuta) con número PROPIO: por cada ventana grabada,
un task best-effort re-chequea el book en memoria a T+200ms y T+1s. Gate
pre-registrado: n≥200 ventanas, <5% sobrevive 200ms → ⚫ de M1 cerrado con mecánica
+ dato propio, sin re-litigio. Cero órdenes, cero red, cero contaminación del mes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import select

from src.storage.models import EdgeWindow, get_session
from src.strategies.motor_1_arbitrage.engine import Motor1Engine


def _top(price: int, size: int = 50):
    t = MagicMock()
    t.best_bid.price_cents = price
    t.best_bid.size = size
    return t


def _engine_con_cruce() -> Motor1Engine:
    """Engine SHADOW con un cruce detectable en 'TICK' (yes_bid 60 + no_bid 45 → 95<100)."""
    manager = MagicMock()
    manager.tracked_tickers = ["TICK"]
    manager.get_top_of_book.side_effect = lambda t, side: _top(60) if side == "yes" else _top(45)
    manager.book_trust_info.return_value = None

    settings = MagicMock()
    settings.TRADING_ENABLED = False
    settings.MIN_EDGE_PCT = 1.0
    settings.MOTOR_1_EXECUTION_EDGE_PCT = 1.0
    settings.MOTOR_1_BOOK_TRUST_SEC = 0.0
    with patch("src.strategies.motor_1_arbitrage.engine.get_settings", return_value=settings):
        engine = Motor1Engine(manager, executor=None)
    engine.settings = settings
    return engine


async def _esperar_tasks(engine: Motor1Engine) -> None:
    while engine._survival_tasks:
        await asyncio.gather(*list(engine._survival_tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_cruce_que_muere_queda_medido_como_muerto(monkeypatch, tmp_db_engine):
    """EL CASO PREDICHO: la ventana se graba, y para T+200ms el cruce ya no existe
    → survived_200ms=False, survived_1s=False. El número que cierra el ⚫."""
    monkeypatch.setattr(
        Motor1Engine, "SURVIVAL_CHECKS", ((0.01, "survived_200ms"), (0.02, "survived_1s"))
    )
    engine = _engine_con_cruce()
    await engine._tick()  # graba la ventana y lanza el task

    # El cruce muere inmediatamente (el matching engine lo consumió): book normal.
    engine._manager.get_top_of_book.side_effect = lambda t, side: (
        _top(48) if side == "yes" else _top(48)  # 48+48=96 → asks sintéticos 52+52>100
    )
    await _esperar_tasks(engine)

    with get_session() as s:
        row = list(s.exec(select(EdgeWindow)))[0]
    assert row.survived_200ms is False
    assert row.survived_1s is False


@pytest.mark.asyncio
async def test_cruce_persistente_queda_medido_como_vivo(monkeypatch, tmp_db_engine):
    """CONTROL: si el cruce SIGUIERA vivo (refutación de la predicción), la telemetría
    lo diría — la medición puede fallar en ambas direcciones o no es medición."""
    monkeypatch.setattr(
        Motor1Engine, "SURVIVAL_CHECKS", ((0.01, "survived_200ms"), (0.02, "survived_1s"))
    )
    engine = _engine_con_cruce()
    await engine._tick()
    await _esperar_tasks(engine)  # el book jamás cambió: el cruce sigue

    with get_session() as s:
        row = list(s.exec(select(EdgeWindow)))[0]
    assert row.survived_200ms is True
    assert row.survived_1s is True


@pytest.mark.asyncio
async def test_cola_llena_saltea_y_cuenta(monkeypatch):
    """NADA SIN COTA: con SURVIVAL_MAX_TASKS tasks vivos, la medición nueva se saltea
    (contada) en vez de acumular tasks sin tope."""
    engine = _engine_con_cruce()
    engine._survival_tasks = {MagicMock() for _ in range(Motor1Engine.SURVIVAL_MAX_TASKS)}

    engine._lanzar_supervivencia(999, "TICK")

    assert engine._survival_skipped == 1


@pytest.mark.asyncio
async def test_fallo_de_medicion_no_rompe_nada(monkeypatch):
    """LECCIÓN 7: un error midiendo (DB caída) se loguea y muere en su task —
    jamás toca el tick ni la detección."""
    monkeypatch.setattr(Motor1Engine, "SURVIVAL_CHECKS", ((0.0, "survived_200ms"),))
    engine = _engine_con_cruce()
    with patch(
        "src.strategies.motor_1_arbitrage.engine.get_session", side_effect=RuntimeError("db")
    ):
        # La grabación de la ventana también usa get_session → falla best-effort y el
        # tick sigue; no debe propagar nada.
        await engine._tick()
        await _esperar_tasks(engine)
    assert True  # llegar acá sin excepción ES el test
