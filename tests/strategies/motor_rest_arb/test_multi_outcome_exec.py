"""
Tests de la EJECUCIÓN del arb multi-outcome (1X2) — el profit LOCKEADO desbloqueado.

Cubre: (a) el engine dispara el executor cuando hay arb + TRADING_ENABLED + edge sobre
umbral, con un opp de N patas todas YES; (b) el gate de shadow (TRADING_ENABLED=false)
sigue sin ejecutar; (c) el RestExecutor persiste N filas DISTINTAS para un opp all-yes
(la unicidad de coid que antes colisionaba). DB temporal: conftest del paquete (autouse).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.risk.manager import TradeDecision
from src.strategies.motor_rest_arb.engine import RestArbEngine
from src.strategies.motor_rest_arb.executor import ExecutionOutcome, LegState, RestExecutor

EV = "KXWCGAME-26JUN27JORARG"
T_ARG, T_JOR, T_TIE = f"{EV}-ARG", f"{EV}-JOR", f"{EV}-TIE"


def _engine(*, executor=None, risk_manager=None, trading_enabled=True) -> RestArbEngine:
    settings = MagicMock()
    settings.MOTOR_REST_MIN_EDGE_CENTS = 1
    settings.MOTOR_REST_MIN_DEPTH = 2
    settings.MOTOR_REST_EXECUTION_EDGE_PCT = 0.0  # umbral bajo: cualquier arb viable ejecuta
    settings.MOTOR_REST_MAX_EDGE_PCT = 10_000.0  # techo alto: no interfiere en estos tests
    # Universo multi ahora viene de config (defaults de producción).
    settings.MOTOR_REST_MULTI_SERIES = "KXWCGAME,KXWCGROUPWIN,KXMENWORLDCUP,KXMWORLDCUP"
    settings.MOTOR_REST_MULTI_MAX_QUOTE_AGE_SEC = 30.0
    settings.MOTOR_REST_MULTI_MIN_LEGS = 3
    settings.TRADING_ENABLED = trading_enabled
    with patch("src.strategies.motor_rest_arb.engine.get_settings", return_value=settings):
        eng = RestArbEngine(executor=executor, risk_manager=risk_manager)
    eng.update_universe({T_ARG, T_JOR, T_TIE})
    return eng


def _tick(ticker: str, ask_dollars: str) -> dict:
    return {
        "type": "ticker",
        "msg": {
            "market_ticker": ticker,
            "yes_bid_dollars": "0.05",
            "yes_ask_dollars": ask_dollars,
            "yes_bid_size_fp": "50.00",
            "yes_ask_size_fp": "50.00",
        },
    }


def _outcome() -> ExecutionOutcome:
    return ExecutionOutcome(filled=True, leg_states=[LegState.FILL, LegState.FILL, LegState.FILL])


async def _feed_full_group(eng: RestArbEngine, asks=("0.30", "0.30", "0.30")) -> None:
    await eng.on_ticker(_tick(T_ARG, asks[0]))
    await eng.on_ticker(_tick(T_JOR, asks[1]))
    await eng.on_ticker(_tick(T_TIE, asks[2]))  # 3er tick completa el grupo → evalúa
    if eng._exec_task is not None:
        await eng._exec_task  # dejar terminar la task de ejecución lanzada


@pytest.mark.asyncio
async def test_multi_executes_three_yes_legs_when_live_and_approved():
    """Σasks=90<100 + TRADING_ENABLED + riesgo aprueba → executor.execute con 3 patas YES."""
    ex = MagicMock()
    ex.execute = AsyncMock(return_value=_outcome())
    risk = MagicMock()
    risk.check_pre_trade = AsyncMock(return_value=TradeDecision(True, "Aprobado", 5))

    eng = _engine(executor=ex, risk_manager=risk, trading_enabled=True)
    await _feed_full_group(eng)

    ex.execute.assert_awaited_once()
    opp = ex.execute.await_args.args[0]
    assert len(opp.legs) == 3
    assert all(leg.side == "yes" for leg in opp.legs)
    assert {leg.market_ticker for leg in opp.legs} == {T_ARG, T_JOR, T_TIE}


@pytest.mark.asyncio
async def test_multi_shadow_does_not_execute_when_trading_disabled():
    """TRADING_ENABLED=false → aunque haya arb y executor, JAMÁS ejecuta (shadow)."""
    ex = MagicMock()
    ex.execute = AsyncMock(return_value=_outcome())
    eng = _engine(executor=ex, risk_manager=MagicMock(), trading_enabled=False)
    await _feed_full_group(eng)
    ex.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_risk_rejection_does_not_execute():
    """El RiskManager rechaza → no se coloca ninguna orden."""
    ex = MagicMock()
    ex.execute = AsyncMock(return_value=_outcome())
    risk = MagicMock()
    risk.check_pre_trade = AsyncMock(return_value=TradeDecision(False, "Exposición alcanzada", 0))

    eng = _engine(executor=ex, risk_manager=risk, trading_enabled=True)
    await _feed_full_group(eng)
    ex.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_persists_distinct_rows_for_all_yes_multi():
    """REGRESIÓN coid: un opp de 3 patas YES persiste 3 filas DISTINTAS (no colisión)."""
    legs = tuple(
        ArbLeg(market_ticker=t, side="yes", price_cents=30, count=5, available_size=100)
        for t in (T_ARG, T_JOR, T_TIE)
    )
    opp = ArbOpportunity(
        legs=legs, count=5, gross_profit_cents=50, fees_cents=6, net_profit_cents=44, edge_pct=1.5
    )
    client = AsyncMock()
    client.place_order = AsyncMock(
        return_value={"order": {"order_id": "X", "fill_count": 5, "remaining_count": 0}}
    )
    ex = RestExecutor(client)

    outcome = await ex.execute(opp)

    assert outcome.filled is True  # 3/3 FILL
    assert client.place_order.await_count == 3
    with models.get_session() as s:
        rows = list(s.exec(select(models.Trade)).all())
    assert len(rows) == 3
    assert len({r.client_order_id for r in rows}) == 3  # coids ÚNICOS (sin colisión)
    assert all(r.side == "yes" and r.strategy == "motor_rest_arb" for r in rows)
    # Todas comparten el mismo arb_id en notes (grupo de settlement).
    assert len({r.notes for r in rows}) == 1
