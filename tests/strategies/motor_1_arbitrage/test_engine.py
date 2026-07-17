"""
Tests del FLUJO de Motor1Engine (el muro Capa A/B + single-flight + loop supervisado).

execute() está mockeado (no toca red). Se valida que se LLAMA (o no) según el muro:
  - shadow (executor=None): detecta + graba, NUNCA ejecuta (Capa B).
  - live (executor + TRADING_ENABLED): arb detectado → execute llamado.
  - por debajo de MOTOR_1_EXECUTION_EDGE_PCT → skip.
  - por encima de MOTOR_1_MAX_EDGE_PCT → graba pero skip (anti-fantasma).
  - single-flight ocupado → skip.
  - el loop run() sale limpio al setear el stop_event.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.strategies.motor_1_arbitrage.engine import Motor1Engine
from src.strategies.motor_1_arbitrage.orderbook import BookLevel, BookTop


def _settings(
    *,
    trading: bool,
    exec_edge_pct: float = 0.0,
    max_edge_pct: float = 10_000.0,
    confirm_ticks: int = 1,
    cooldown_sec: float = 60.0,
):
    s = MagicMock()
    s.MIN_EDGE_PCT = 0.0
    s.MOTOR_1_EXECUTION_EDGE_PCT = exec_edge_pct
    s.MOTOR_1_MAX_EDGE_PCT = max_edge_pct
    # confirm_ticks=1 por default en los tests preexistentes: ejecutan al primer tick
    # (la confirmación de persistencia se testea explícita en test_confirm_*).
    s.MOTOR_1_CONFIRM_TICKS = confirm_ticks
    s.MOTOR_1_TICKER_COOLDOWN_SEC = cooldown_sec
    s.TRADING_ENABLED = trading
    return s


def _arb_manager() -> MagicMock:
    """Book cruzado: yes_bid=60/no_bid=55 → asks sintéticos 45+40=85<100 → arb rentable."""
    m = MagicMock()
    yes_top = BookTop(best_bid=BookLevel(price_cents=60, size=100), best_ask=None)
    no_top = BookTop(best_bid=BookLevel(price_cents=55, size=100), best_ask=None)
    m.get_top_of_book.side_effect = lambda t, side: yes_top if side == "yes" else no_top
    m.tracked_tickers = frozenset({"KXTEST"})
    return m


def _engine(settings, *, manager=None, executor=None) -> Motor1Engine:
    with patch("src.strategies.motor_1_arbitrage.engine.get_settings", return_value=settings):
        return Motor1Engine(manager or _arb_manager(), executor=executor)


def _opp(edge_pct: float) -> ArbOpportunity:
    legs = (
        ArbLeg(market_ticker="KXTEST", side="yes", price_cents=45, count=30, available_size=30),
        ArbLeg(market_ticker="KXTEST", side="no", price_cents=40, count=30, available_size=30),
    )
    return ArbOpportunity(
        legs=legs,
        count=30,
        gross_profit_cents=450,
        fees_cents=50,
        net_profit_cents=400,
        edge_pct=edge_pct,
    )


def _mock_executor() -> MagicMock:
    ex = MagicMock()
    ex.execute = AsyncMock(return_value=True)
    return ex


async def _drain(engine: Motor1Engine) -> None:
    task = engine._exec_task
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_shadow_never_executes(tmp_db_engine):
    """executor=None (shadow): se detecta y graba, pero Capa B corta — execute jamás corre."""
    eng = _engine(_settings(trading=False))  # executor None
    await eng._tick()
    await _drain(eng)

    assert eng._signals_seen == 1  # detectó el arb real (book cruzado)
    assert eng._exec_task is None  # nunca lanzó task de ejecución
    assert eng._executing is False


@pytest.mark.asyncio
async def test_live_executes_full_flow(tmp_db_engine):
    """flag=true + executor: arb detectado → execute llamado con el opp detectado."""
    ex = _mock_executor()
    eng = _engine(_settings(trading=True, exec_edge_pct=0.0), executor=ex)
    with patch.object(eng, "_detect", return_value=_opp(edge_pct=5.0)):
        await eng._tick()
        await _drain(eng)

    ex.execute.assert_awaited_once()
    executed_opp = ex.execute.call_args.args[0]
    assert executed_opp.edge_pct == 5.0
    assert eng._executing is False  # limpiado en finally
    assert eng._exec_task is None


@pytest.mark.asyncio
async def test_below_fine_threshold_does_not_execute(tmp_db_engine):
    """Umbral fino alto: el arb se detecta/graba pero no supera EXECUTION_EDGE_PCT → no ejecuta."""
    ex = _mock_executor()
    eng = _engine(_settings(trading=True, exec_edge_pct=99.0), executor=ex)
    with patch.object(eng, "_detect", return_value=_opp(edge_pct=5.0)):
        await eng._tick()
        await _drain(eng)

    ex.execute.assert_not_awaited()
    assert eng._signals_seen == 1  # detectó y grabó


@pytest.mark.asyncio
async def test_above_max_edge_does_not_execute(tmp_db_engine):
    """Techo anti-fantasma: edge por encima de MOTOR_1_MAX_EDGE_PCT → graba pero NO ejecuta."""
    ex = _mock_executor()
    eng = _engine(_settings(trading=True, exec_edge_pct=0.0, max_edge_pct=1.0), executor=ex)
    with patch.object(eng, "_detect", return_value=_opp(edge_pct=50.0)):
        await eng._tick()
        await _drain(eng)

    assert eng._signals_seen == 1  # detectó y grabó el EdgeWindow
    ex.execute.assert_not_awaited()  # pero el techo cortó la ejecución
    assert eng._exec_task is None


@pytest.mark.asyncio
async def test_single_flight_discards_while_busy(tmp_db_engine):
    """Si ya hay una ejecución en curso (_executing=True), el nuevo arb se descarta."""
    ex = _mock_executor()
    eng = _engine(_settings(trading=True, exec_edge_pct=0.0), executor=ex)
    eng._executing = True  # simular ejecución en curso
    with patch.object(eng, "_detect", return_value=_opp(edge_pct=5.0)):
        await eng._tick()
        await _drain(eng)

    ex.execute.assert_not_awaited()
    assert eng._exec_task is None  # no se lanzó un segundo task


@pytest.mark.asyncio
async def test_run_loop_exits_on_stop_event(tmp_db_engine):
    """run() sale limpio cuando el stop_event ya está seteado (no cuelga)."""
    eng = _engine(_settings(trading=False))
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(eng.run(stop), timeout=2.0)
    assert eng._exec_task is None


@pytest.mark.asyncio
async def test_tick_failure_does_not_kill_loop(tmp_db_engine):
    """Un _tick() que lanza se registra (BotState.record_error) y el loop SIGUE/sale limpio."""
    eng = _engine(_settings(trading=False))
    stop = asyncio.Event()
    calls = {"n": 0}

    async def _boom() -> None:
        calls["n"] += 1
        stop.set()  # salir tras el primer tick fallido
        raise RuntimeError("tick boom")

    with (
        patch.object(eng, "_tick", side_effect=_boom),
        patch("src.strategies.motor_1_arbitrage.engine.BotState.record_error") as rec,
    ):
        await asyncio.wait_for(eng.run(stop), timeout=2.0)

    assert calls["n"] == 1
    rec.assert_called_once()  # el fallo del tick se registró, no se propagó


# =====================================================
# Confirmación de persistencia + cooldown (auditoría rentabilidad 2026-07-07:
# el cruce binario intra-ticker de 1 tick es casi siempre book stale)
# =====================================================


@pytest.mark.asyncio
async def test_confirm_ticks_waits_for_persistent_cross(tmp_db_engine):
    """confirm_ticks=2: el primer tick NO ejecuta (streak 1/2); el segundo, con el cruce
    aún vivo, SÍ. El fantasma de 1 tick jamás llega a la red."""
    ex = _mock_executor()
    eng = _engine(_settings(trading=True, confirm_ticks=2), executor=ex)
    with patch.object(eng, "_detect", return_value=_opp(edge_pct=5.0)):
        await eng._tick()
        await _drain(eng)
        ex.execute.assert_not_awaited()  # streak 1/2 → espera
        await eng._tick()
        await _drain(eng)
    ex.execute.assert_awaited_once()  # streak 2/2 → ejecuta


@pytest.mark.asyncio
async def test_confirm_streak_resets_when_cross_dies(tmp_db_engine):
    """CONTROL: si el cruce desaparece un tick, la racha vuelve a 0 — dos avistajes NO
    consecutivos no confirman."""
    ex = _mock_executor()
    eng = _engine(_settings(trading=True, confirm_ticks=2), executor=ex)
    opp = _opp(edge_pct=5.0)
    with patch.object(eng, "_detect", side_effect=[opp, None, opp]):
        await eng._tick()  # streak 1
        await eng._tick()  # cruce muerto → reset
        await eng._tick()  # streak 1 de nuevo
        await _drain(eng)
    ex.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_after_failed_execution(tmp_db_engine):
    """Ejecución fallida (KILL/rollback) → cooldown: el mismo ticker no se re-ejecuta
    aunque el cruce siga vivo (book stale re-martillado era el modo de falla)."""
    ex = _mock_executor()
    ex.execute = AsyncMock(return_value=False)  # falla
    eng = _engine(_settings(trading=True, cooldown_sec=999.0), executor=ex)
    with patch.object(eng, "_detect", return_value=_opp(edge_pct=5.0)):
        await eng._tick()
        await _drain(eng)
        assert ex.execute.await_count == 1
        await eng._tick()  # cruce sigue "vivo" pero el ticker está en cooldown
        await _drain(eng)
    assert ex.execute.await_count == 1  # NO se re-ejecutó


@pytest.mark.asyncio
async def test_successful_execution_no_cooldown(tmp_db_engine):
    """CONTROL: una ejecución EXITOSA no impone cooldown — un cruce legítimo repetido
    (book profundo) se puede volver a capturar."""
    ex = _mock_executor()  # execute → True
    eng = _engine(_settings(trading=True, cooldown_sec=999.0), executor=ex)
    with patch.object(eng, "_detect", return_value=_opp(edge_pct=5.0)):
        await eng._tick()
        await _drain(eng)
        await eng._tick()
        await _drain(eng)
    assert ex.execute.await_count == 2
