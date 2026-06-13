"""
Tests del FLUJO DE EJECUCIÓN del Motor REST (cable Capa 2).

Cubren el muro y el flujo: en shadow (executor=None) NUNCA se ejecuta (Capa B);
con flag + executor corre detección → umbral fino → check_pre_trade → resize →
execute. Y los cortes: umbral no superado, single-flight ocupado, rechazo de riesgo,
resize no viable. execute() está mockeado (no toca red); se valida que se LLAMA (o no).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.risk.manager import TradeDecision
from src.strategies.motor_rest_arb.engine import RestArbEngine
from src.strategies.motor_rest_arb.executor import ExecutionOutcome, LegState


def _settings(*, trading: bool, exec_edge_pct: float = 0.0) -> MagicMock:
    s = MagicMock()
    s.MOTOR_REST_MIN_EDGE_CENTS = 1
    s.MOTOR_REST_MIN_DEPTH = 2
    s.MOTOR_REST_EXECUTION_EDGE_PCT = exec_edge_pct
    s.TRADING_ENABLED = trading
    return s


def _engine(settings: MagicMock, *, executor=None, risk_manager=None) -> RestArbEngine:
    with patch("src.strategies.motor_rest_arb.engine.get_settings", return_value=settings):
        return RestArbEngine(executor=executor, risk_manager=risk_manager)


def _arb_ticker() -> dict:
    """Ticker con libro cruzado (yes_bid > yes_ask) → arbitraje YES+NO rentable."""
    return {
        "type": "ticker",
        "msg": {
            "market_ticker": "KXTEST",
            "yes_bid_dollars": "0.60",  # no_ask = 100 - 60 = 40
            "yes_ask_dollars": "0.55",  # yes_ask + no_ask = 95 < 100 → arb
            "yes_bid_size_fp": "100.00",
            "yes_ask_size_fp": "100.00",
        },
    }


async def _drain(engine: RestArbEngine) -> None:
    """Espera al task de ejecución lanzado por on_ticker (si lo hubo)."""
    task = engine._exec_task
    if task is not None:
        await task


def _mock_executor() -> MagicMock:
    ex = MagicMock()
    ex.execute = AsyncMock(
        return_value=ExecutionOutcome(filled=True, leg_states=[LegState.FILL, LegState.FILL])
    )
    return ex


def _mock_risk(approved: bool = True, max_count: int = 50) -> MagicMock:
    rm = MagicMock()
    rm.check_pre_trade = AsyncMock(return_value=TradeDecision(approved, "ok", max_count))
    return rm


@pytest.mark.asyncio
async def test_shadow_never_executes():
    """executor=None (shadow): se detecta y graba, pero Capa B corta — execute jamás corre."""
    eng = _engine(_settings(trading=False))  # executor/risk_manager None
    with patch.object(eng, "_record_edge_window", return_value=123):
        await eng.on_ticker(_arb_ticker())
        await _drain(eng)

    assert eng._signals_seen == 1  # detectó el arb (grabó EdgeWindow)
    assert eng._exec_task is None  # nunca lanzó task de ejecución
    assert eng._executing is False


@pytest.mark.asyncio
async def test_live_executes_full_flow():
    """flag=true + executor: detección → check_pre_trade → resize → execute → alert + update."""
    ex, rm = _mock_executor(), _mock_risk(max_count=50)
    eng = _engine(_settings(trading=True), executor=ex, risk_manager=rm)
    with (
        patch.object(eng, "_record_edge_window", return_value=7),
        patch("src.strategies.motor_rest_arb.engine.alert_trade", new=AsyncMock()) as mock_alert,
        patch.object(eng, "_update_edge_window_outcome") as mock_update,
    ):
        await eng.on_ticker(_arb_ticker())
        await _drain(eng)

    rm.check_pre_trade.assert_awaited_once()
    ex.execute.assert_awaited_once()
    # El opp ejecutado fue redimensionado al count autorizado (50), no el detectado (100).
    executed_opp = ex.execute.call_args.args[0]
    assert executed_opp.count == 50
    # Capa 3: Telegram + update de EdgeWindow corren tras el outcome, con el edge_id
    # de la detección y la latencia medida alrededor de execute().
    mock_alert.assert_awaited_once()
    assert mock_alert.call_args.args[0] is ex.execute.return_value  # el outcome real
    mock_update.assert_called_once()
    upd_args = mock_update.call_args.args
    assert upd_args[0] == 7  # edge_id de la detección
    assert upd_args[1] is ex.execute.return_value  # el outcome
    assert isinstance(upd_args[2], int) and upd_args[2] >= 0  # cycle_latency_ms medido
    assert eng._executing is False  # limpiado en finally
    assert eng._exec_task is None


@pytest.mark.asyncio
async def test_below_fine_threshold_does_not_execute():
    """Umbral fino alto: el arb se detecta pero no supera EXECUTION_EDGE_PCT → no ejecuta."""
    ex, rm = _mock_executor(), _mock_risk()
    eng = _engine(_settings(trading=True, exec_edge_pct=99.0), executor=ex, risk_manager=rm)
    with patch.object(eng, "_record_edge_window", return_value=1):
        await eng.on_ticker(_arb_ticker())
        await _drain(eng)

    ex.execute.assert_not_awaited()
    rm.check_pre_trade.assert_not_awaited()


@pytest.mark.asyncio
async def test_single_flight_discards_while_busy():
    """Si ya hay una ejecución en curso (_executing=True), el nuevo arb se descarta."""
    ex, rm = _mock_executor(), _mock_risk()
    eng = _engine(_settings(trading=True), executor=ex, risk_manager=rm)
    eng._executing = True  # simular ejecución en curso
    with patch.object(eng, "_record_edge_window", return_value=1):
        await eng.on_ticker(_arb_ticker())
        await _drain(eng)

    ex.execute.assert_not_awaited()
    assert eng._exec_task is None  # no se lanzó un segundo task


@pytest.mark.asyncio
async def test_risk_rejection_blocks_execution():
    """check_pre_trade rechaza → execute() no corre."""
    ex, rm = _mock_executor(), _mock_risk(approved=False, max_count=0)
    eng = _engine(_settings(trading=True), executor=ex, risk_manager=rm)
    with patch.object(eng, "_record_edge_window", return_value=1):
        await eng.on_ticker(_arb_ticker())
        await _drain(eng)

    rm.check_pre_trade.assert_awaited_once()
    ex.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_resize_not_viable_blocks_execution():
    """Riesgo aprueba con max_count=0 → resize devuelve None → no ejecuta (fail-safe)."""
    ex, rm = _mock_executor(), _mock_risk(approved=True, max_count=0)
    eng = _engine(_settings(trading=True), executor=ex, risk_manager=rm)
    with patch.object(eng, "_record_edge_window", return_value=1):
        await eng.on_ticker(_arb_ticker())
        await _drain(eng)

    rm.check_pre_trade.assert_awaited_once()
    ex.execute.assert_not_awaited()  # resize None → fail-safe, no ejecuta


# ── _resize_opportunity: mapeo de patas (corazón del cable) ──────────────────────
# Patas ASIMÉTRICAS (precio Y size distintos en YES vs NO) → un mapeo cruzado
# (yes_leg→no_*) produciría precios/sizes intercambiados en el opp resultante y el
# assert falla. Mocks simétricos NO atraparían esto.


def _asym_opp(*, reversed_order: bool) -> ArbOpportunity:
    """opp binario con YES (ask 55, depth 80) y NO (ask 40, depth 100) — asimétrico."""
    yes_leg = ArbLeg(
        market_ticker="KXTEST", side="yes", price_cents=55, count=80, available_size=80
    )
    no_leg = ArbLeg(
        market_ticker="KXTEST", side="no", price_cents=40, count=100, available_size=100
    )
    legs = (no_leg, yes_leg) if reversed_order else (yes_leg, no_leg)
    # gross/fees/net/edge dummy: el resize los RECOMPUTA vía detect_binary_arb.
    return ArbOpportunity(
        legs=legs, count=80, gross_profit_cents=0, fees_cents=0, net_profit_cents=0, edge_pct=0.0
    )


@pytest.mark.parametrize("reversed_order", [False, True])
def test_resize_maps_legs_by_side_not_order(reversed_order: bool):
    """El resize mapea por side (no por índice) y preserva la inversión cross-side."""
    eng = _engine(_settings(trading=True))
    resized = eng._resize_opportunity(_asym_opp(reversed_order=reversed_order), max_count=30)

    assert resized is not None
    r_yes = next(leg for leg in resized.legs if leg.side == "yes")
    r_no = next(leg for leg in resized.legs if leg.side == "no")
    # YES conserva SU precio (55) y SU profundidad (80); NO conserva los suyos (40, 100).
    # Si el mapeo se cruzara, estos precios/sizes saldrían intercambiados → falla.
    assert (r_yes.price_cents, r_yes.available_size) == (55, 80)
    assert (r_no.price_cents, r_no.available_size) == (40, 100)
    # count autorizado = min(80, 100, 30) = 30 (recomputado, no el 80 original).
    assert resized.count == 30


def test_resize_returns_none_when_count_below_one():
    """max_count < 1 → None (fail-safe), nunca un opp de tamaño 0."""
    eng = _engine(_settings(trading=True))
    assert eng._resize_opportunity(_asym_opp(reversed_order=False), max_count=0) is None
