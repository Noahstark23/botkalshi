"""
Tests del multi-outcome SHADOW (P3) + near-miss (P4) — el negocio real del fútbol.

Verifica el invariante de seguridad central: el grupo se evalúa SOLO completo y fresco
(un grupo parcial/stale haría parecer arb lo que no lo es), que la detección graba
EdgeWindow kind="multi_outcome", y que el path multi JAMÁS toca el executor (shadow).
La DB temporal la monta el conftest del paquete (autouse).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.strategies.motor_rest_arb.engine import RestArbEngine

# Fixture REAL verificado contra la API (2026-06-13): el evento 1X2 tiene exactamente
# 3 markets con sufijo de outcome en el último segmento.
EV = "KXWCGAME-26JUN27JORARG"
T_ARG, T_MEX, T_TIE = f"{EV}-ARG", f"{EV}-JOR", f"{EV}-TIE"


def _engine(*, executor=None, risk_manager=None) -> RestArbEngine:
    settings = MagicMock()
    settings.MOTOR_REST_MIN_EDGE_CENTS = 1
    settings.MOTOR_REST_MIN_DEPTH = 2
    settings.MOTOR_REST_EXECUTION_EDGE_PCT = 1.5
    settings.TRADING_ENABLED = False
    with patch("src.strategies.motor_rest_arb.engine.get_settings", return_value=settings):
        eng = RestArbEngine(executor=executor, risk_manager=risk_manager)
    eng.update_universe({T_ARG, T_MEX, T_TIE, "KXNBA-26-LAL"})  # NBA queda fuera (whitelist)
    return eng


def _tick(ticker: str, ask_dollars: str, bid_dollars: str = "0.05") -> dict:
    return {
        "type": "ticker",
        "msg": {
            "market_ticker": ticker,
            "yes_bid_dollars": bid_dollars,
            "yes_ask_dollars": ask_dollars,
            "yes_bid_size_fp": "50.00",
            "yes_ask_size_fp": "50.00",
        },
    }


def _multi_windows() -> list[models.EdgeWindow]:
    with models.get_session() as s:
        rows = list(s.exec(select(models.EdgeWindow)).all())
    return [w for w in rows if w.kind == "multi_outcome"]


def test_update_universe_groups_by_event_and_filters_whitelist():
    eng = _engine()
    assert eng._event_universe == {EV: {T_ARG, T_MEX, T_TIE}}  # NBA filtrada
    assert eng._ticker_to_event[T_ARG] == EV


def test_series_match_is_exact_not_prefix():
    """KXWCGAMEGOALS (totales, NO excluyentes) NO debe caer en KXWCGAME por prefijo."""
    eng = _engine()
    eng.update_universe({
        f"{EV}-ARG", f"{EV}-JOR", f"{EV}-TIE",                      # 1X2 real → entra
        "KXWCGAMEGOALS-26JUN27JORARG-O25", "KXWCGAMEGOALS-26JUN27JORARG-U25",  # totales → FUERA
    })
    assert set(eng._event_universe) == {EV}
    assert all(not t.startswith("KXWCGAMEGOALS") for ts in eng._event_universe.values() for t in ts)


@pytest.mark.asyncio
async def test_complete_fresh_1x2_under_100_records_multi_window():
    """1X2 completo y fresco con Σasks=95 < 100 → EdgeWindow kind=multi_outcome."""
    eng = _engine()
    await eng.on_ticker(_tick(T_ARG, "0.40"))
    await eng.on_ticker(_tick(T_MEX, "0.30"))
    assert _multi_windows() == []          # con 2/3 patas todavía NO evalúa
    await eng.on_ticker(_tick(T_TIE, "0.25"))  # Σ = 95

    wins = _multi_windows()
    assert len(wins) == 1
    w = wins[0]
    assert w.market_ticker == EV
    assert w.gross_spread_cents == (100 - 95) * w.count  # margen bruto del grupo
    assert w.magnitude_cents > 0 and w.count > 0 and w.edge_pct > 0
    assert eng._multi_signals_seen == 1


@pytest.mark.asyncio
async def test_incomplete_group_never_evaluates():
    """Falta un outcome del evento → NUNCA se evalúa (la señal falsa es imposible)."""
    eng = _engine()
    # Σ de las 2 presentes = 55 (<100), pero falta TIE → sin esa pata NO es arb.
    await eng.on_ticker(_tick(T_ARG, "0.30"))
    await eng.on_ticker(_tick(T_MEX, "0.25"))
    assert _multi_windows() == []


@pytest.mark.asyncio
async def test_stale_leg_blocks_evaluation():
    """Una pata con quote vieja (> MULTI_MAX_QUOTE_AGE_SEC) bloquea el grupo entero."""
    eng = _engine()
    await eng.on_ticker(_tick(T_ARG, "0.40"))
    await eng.on_ticker(_tick(T_MEX, "0.30"))
    # Envejecer la quote de ARG más allá del límite de frescura.
    ask, size, ts = eng._quote_cache[T_ARG]
    eng._quote_cache[T_ARG] = (ask, size, ts - eng.MULTI_MAX_QUOTE_AGE_SEC - 1)
    await eng.on_ticker(_tick(T_TIE, "0.25"))
    assert _multi_windows() == []


@pytest.mark.asyncio
async def test_sum_at_or_above_100_records_nothing():
    """Σasks ≥ 100 → mercado eficiente, sin ventana (pero el near-miss lo registra)."""
    eng = _engine()
    await eng.on_ticker(_tick(T_ARG, "0.40"))
    await eng.on_ticker(_tick(T_MEX, "0.35"))
    await eng.on_ticker(_tick(T_TIE, "0.30"))  # Σ = 105
    assert _multi_windows() == []
    assert eng._best_multi_sum is not None
    assert eng._best_multi_sum[0] == 105 and eng._best_multi_sum[2] == EV


@pytest.mark.asyncio
async def test_multi_path_never_touches_executor():
    """SHADOW estricto: aunque haya arb multi, el executor JAMÁS se invoca por este path."""
    ex = MagicMock()
    ex.execute = AsyncMock()
    eng = _engine(executor=ex, risk_manager=MagicMock())
    await eng.on_ticker(_tick(T_ARG, "0.40"))
    await eng.on_ticker(_tick(T_MEX, "0.30"))
    await eng.on_ticker(_tick(T_TIE, "0.25"))  # arb multi detectado y grabado

    assert len(_multi_windows()) == 1      # detectó y grabó
    ex.execute.assert_not_awaited()        # pero NUNCA ejecutó


@pytest.mark.asyncio
async def test_near_miss_binary_tracked_in_window():
    """P4: el gap binario mínimo (ask−bid) queda registrado para el heartbeat."""
    eng = _engine()
    await eng.on_ticker(_tick("KXNBA-26-LAL", "0.55", bid_dollars="0.56"))  # CRUZADO: gap −1
    assert eng._best_binary_gap is not None
    gap, ticker = eng._best_binary_gap
    assert gap == -1 and ticker == "KXNBA-26-LAL"


@pytest.mark.asyncio
async def test_heartbeat_reports_multi_and_near_miss():
    """El heartbeat incluye las señales multi y los near-miss de la ventana."""
    from loguru import logger as _logger

    eng = _engine()
    eng.HEARTBEAT_EVERY = 3
    records: list[str] = []
    sink = _logger.add(records.append, level="INFO", format="{message}")
    try:
        await eng.on_ticker(_tick(T_ARG, "0.40"))
        await eng.on_ticker(_tick(T_MEX, "0.30"))
        await eng.on_ticker(_tick(T_TIE, "0.25"))  # 3er tick → heartbeat
    finally:
        _logger.remove(sink)

    hb = [r for r in records if "REST Engine Heartbeat" in r]
    assert len(hb) == 1
    assert "multi: 1 señales" in hb[0]
    assert "near-miss" in hb[0] and EV in hb[0]
