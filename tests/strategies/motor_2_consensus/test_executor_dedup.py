"""
Dedup CROSS-CICLO del Motor2Executor (fix auditoría 2026-07-01).

El bug: un edge de consenso persistente (dura horas) se re-apostaba en cada ciclo del
poller (300s) porque nadie consultaba si ya había posición abierta — ~24 compras
apiladas sobre el mismo partido en 2h (la clase de concentración del caso PHI −$218).
MOTOR_2_ONE_BET_PER_EVENT solo colapsaba señales DENTRO de un ciclo.

Verifica: posición viva (pending/filled) en el mismo EVENTO → skip sin tocar la red ni
el RiskManager; fila settled (resuelta o cerrada por exit) → libera el evento; scope
por ticker cuando one_bet_per_event=False o la señal no trae event_key; fallo de DB en
el check → skip conservador.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import src.storage.models as models
from src.risk.manager import TradeDecision
from src.strategies.motor_2_consensus.detector import ConsensusSignal
from src.strategies.motor_2_consensus.executor import Motor2Executor

EVENT = "KXWCGAME-26JUN27JORARG"
TICKER_ARG = f"{EVENT}-ARG"
TICKER_JOR = f"{EVENT}-JOR"


def _signal(
    *, ticker: str = TICKER_ARG, event_key: str = EVENT, price: int = 40
) -> ConsensusSignal:
    return ConsensusSignal(
        market_ticker=ticker,
        event_key=event_key,
        kalshi_side="YES",
        odds_api_fair_prob=0.62,
        kalshi_price_cents=price,
        edge_pct=0.05,
        recommended_size_usd=15.0,
    )


class _FakeRisk:
    def __init__(self):
        self.calls = 0

    async def check_pre_trade(self, opp):
        self.calls += 1
        return TradeDecision(True, "Aprobado", 5)

    async def check_and_reserve(self, opp, persist_intent):
        d = await self.check_pre_trade(opp)
        if d.approved and not persist_intent(d):
            return TradeDecision(False, "persist_intent_failed", 0)
        return d


class _FakeClient:
    def __init__(self):
        self.place_order = AsyncMock(
            return_value={"order": {"order_id": "OK", "fill_count": 5, "remaining_count": 0}}
        )


def _seed_trade(*, ticker: str, status: str, strategy: str = "motor_2_consensus") -> None:
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id=f"seed-{ticker}-{status}",
                ticker=ticker,
                side="yes",
                action="buy",
                count=5,
                price_cents=40,
                strategy=strategy,
                status=status,
            )
        )
        s.commit()


def _trade_count() -> int:
    with models.get_session() as s:
        return len(list(s.exec(select(models.Trade))))


@pytest.mark.asyncio
async def test_open_trade_same_event_other_ticker_blocks():
    """Posición viva en OTRO market del MISMO evento → skip (mismo riesgo direccional)."""
    _seed_trade(ticker=TICKER_JOR, status="filled")
    client, risk = _FakeClient(), _FakeRisk()
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal(ticker=TICKER_ARG))

    assert not outcome.placed and outcome.reason == "already_open"
    client.place_order.assert_not_awaited()
    assert risk.calls == 0  # ni siquiera llega al RiskManager
    assert _trade_count() == 1  # no se escribió intent nuevo


@pytest.mark.asyncio
async def test_pending_trade_blocks_and_second_cycle_skips():
    """Cross-ciclo real: primer execute llena; el segundo (mismo edge, próximo ciclo) skipea."""
    client, risk = _FakeClient(), _FakeRisk()
    ex = Motor2Executor(client, risk)

    first = await ex.execute(_signal())
    assert first.filled

    second = await ex.execute(_signal())
    assert not second.placed and second.reason == "already_open"
    client.place_order.assert_awaited_once()  # la red se tocó UNA vez
    assert _trade_count() == 1


@pytest.mark.asyncio
async def test_settled_trade_releases_event():
    """Fila settled (resuelta o cerrada por exit) → el evento se puede apostar de nuevo."""
    _seed_trade(ticker=TICKER_ARG, status="settled")
    client, risk = _FakeClient(), _FakeRisk()
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal())
    assert outcome.placed and outcome.filled


@pytest.mark.asyncio
async def test_other_strategy_open_trade_does_not_block():
    """Una pata viva de OTRO motor (rest_arb) en el evento no bloquea a Motor 2."""
    _seed_trade(ticker=TICKER_JOR, status="filled", strategy="motor_rest_arb")
    client, risk = _FakeClient(), _FakeRisk()
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal())
    assert outcome.placed and outcome.filled


@pytest.mark.asyncio
async def test_scope_ticker_when_flag_off():
    """one_bet_per_event=False → solo bloquea el MISMO ticker, no el evento entero."""
    _seed_trade(ticker=TICKER_JOR, status="filled")
    client, risk = _FakeClient(), _FakeRisk()
    ex = Motor2Executor(client, risk, one_bet_per_event=False)

    # Otro ticker del evento: pasa.
    assert (await ex.execute(_signal(ticker=TICKER_ARG))).filled
    # El mismo ticker con fila viva: bloquea.
    blocked = await ex.execute(_signal(ticker=TICKER_JOR))
    assert not blocked.placed and blocked.reason == "already_open"


@pytest.mark.asyncio
async def test_signal_without_event_key_falls_back_to_ticker_scope():
    """Señal sin event_key (construida a mano / legacy) → dedup por ticker, no evento."""
    _seed_trade(ticker=TICKER_JOR, status="filled")
    client, risk = _FakeClient(), _FakeRisk()
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal(ticker=TICKER_ARG, event_key=""))
    assert outcome.placed and outcome.filled


@pytest.mark.asyncio
async def test_db_error_in_dedup_check_skips_conservador(monkeypatch):
    """Fallo de DB en el check → skip (mejor perder una señal que apilar exposición ciega)."""
    client, risk = _FakeClient(), _FakeRisk()
    ex = Motor2Executor(client, risk)

    def _boom():
        raise RuntimeError("db caída")

    monkeypatch.setattr("src.strategies.motor_2_consensus.executor.get_session", _boom)
    outcome = await ex.execute(_signal())
    assert not outcome.placed and outcome.reason == "already_open"
    client.place_order.assert_not_awaited()
