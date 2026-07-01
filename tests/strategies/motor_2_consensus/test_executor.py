"""
Tests del Motor2Executor — apuesta SINGLE-LEG direccional del Motor 2.

Verifica: gating del RiskManager, intent PRE-red, fill → Trade 'filled', IOC sin fill →
'cancelled', muro Capa C (TRADING_ENABLED=false) → 'cancelled' sin error, ERROR_RED →
fila queda 'pending' (incierta, sin kill-switch), y que el settlement settlea la apuesta
single-leg (PnL realizado que alimenta el stop-loss). DB temporal: conftest del paquete.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import src.storage.models as models
from src.clients.kalshi_rest import KalshiClientError, TradingDisabledError
from src.risk.manager import TradeDecision
from src.strategies.motor_2_consensus.detector import ConsensusSignal
from src.strategies.motor_2_consensus.executor import Motor2Executor
from src.strategies.motor_rest_arb.settlement import MarketResolution, SettlementPoller

TICKER = "KXWCGAME-26JUN27JORARG-ARG"


def _signal(*, size_usd: float = 15.0, price: int = 40, side: str = "YES") -> ConsensusSignal:
    return ConsensusSignal(
        market_ticker=TICKER,
        kalshi_side=side,
        odds_api_fair_prob=0.62,
        kalshi_price_cents=price,
        edge_pct=0.18,
        recommended_size_usd=size_usd,
    )


class _FakeRisk:
    """RiskManager falso: devuelve la decisión configurada (sin tocar settings/DB)."""

    def __init__(self, decision: TradeDecision):
        self._decision = decision
        self.calls = 0

    async def check_pre_trade(self, opp):
        self.calls += 1
        self.last_opp = opp
        return self._decision

    async def check_and_reserve(self, opp, persist_intent):
        d = await self.check_pre_trade(opp)
        if d.approved and not persist_intent(d):
            return TradeDecision(False, "persist_intent_failed", 0)
        return d


class _FakeClient:
    """KalshiRestClient falso: place_order configurable (resp dict o excepción)."""

    def __init__(self, *, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.place_order = AsyncMock(side_effect=self._place)

    async def _place(self, **kwargs):
        self.last_order = kwargs
        if self._exc is not None:
            raise self._exc
        return self._resp


def _trade() -> models.Trade | None:
    with models.get_session() as s:
        return s.exec(select(models.Trade).where(models.Trade.ticker == TICKER)).first()


@pytest.mark.asyncio
async def test_fill_records_filled_trade():
    client = _FakeClient(resp={"order": {"order_id": "ABC", "fill_count": 5, "remaining_count": 0}})
    risk = _FakeRisk(TradeDecision(True, "Aprobado", 5))
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal(price=40, size_usd=2.0))
    assert outcome.filled and outcome.filled_count == 5
    client.place_order.assert_awaited_once()
    # IOC limit buy del lado correcto, al precio de la señal.
    kw = client.last_order
    assert kw["action"] == "buy" and kw["side"] == "yes"
    assert kw["time_in_force"] == "immediate_or_cancel" and kw["yes_price"] == 40

    t = _trade()
    assert t.status == "filled" and t.count == 5 and t.fill_price_cents == 40
    assert t.strategy == "motor_2_consensus" and t.kalshi_order_id == "ABC"


@pytest.mark.asyncio
async def test_underdog_filter_blocks_cheap_entry_when_enabled():
    """FASE 3: con el flag on, una entrada <40c se bloquea SIN tocar la red ni el riesgo."""
    client = _FakeClient(resp={"order": {}})
    risk = _FakeRisk(TradeDecision(True, "Aprobado", 5))
    ex = Motor2Executor(client, risk, min_entry_cents=40, underdog_filter_enabled=True)

    outcome = await ex.execute(_signal(price=25))
    assert not outcome.placed and not outcome.filled
    assert outcome.reason == "underdog_filter"
    client.place_order.assert_not_awaited()
    assert risk.calls == 0  # ni siquiera consulta al RiskManager
    assert _trade() is None


@pytest.mark.asyncio
async def test_underdog_filter_shadow_logs_but_executes_when_disabled():
    """Shadow intra-live: con el flag off, la entrada <40c se LOGUEA pero igual se ejecuta."""
    client = _FakeClient(resp={"order": {"order_id": "Z", "fill_count": 3, "remaining_count": 0}})
    risk = _FakeRisk(TradeDecision(True, "Aprobado", 3))
    ex = Motor2Executor(client, risk, min_entry_cents=40, underdog_filter_enabled=False)

    outcome = await ex.execute(_signal(price=25, size_usd=1.0))
    assert outcome.filled and outcome.filled_count == 3
    client.place_order.assert_awaited_once()  # NO bloqueó en shadow


@pytest.mark.asyncio
async def test_underdog_filter_allows_entry_at_threshold():
    """El umbral es estricto (price < min): a 40c exacto pasa el filtro y ejecuta."""
    client = _FakeClient(resp={"order": {"order_id": "Y", "fill_count": 2, "remaining_count": 0}})
    risk = _FakeRisk(TradeDecision(True, "Aprobado", 2))
    ex = Motor2Executor(client, risk, min_entry_cents=40, underdog_filter_enabled=True)

    outcome = await ex.execute(_signal(price=40, size_usd=1.0))
    assert outcome.filled and outcome.filled_count == 2
    client.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_risk_rejection_places_no_order():
    client = _FakeClient(resp={"order": {}})
    risk = _FakeRisk(TradeDecision(False, "Límite Exposición alcanzado", 0))
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal())
    assert not outcome.placed and not outcome.filled
    client.place_order.assert_not_awaited()
    assert _trade() is None  # ni siquiera persiste intent si el riesgo rechaza


@pytest.mark.asyncio
async def test_trading_disabled_wall_is_clean_no_position():
    client = _FakeClient(exc=TradingDisabledError("place_order bloqueado: TRADING_ENABLED=false"))
    risk = _FakeRisk(TradeDecision(True, "Aprobado", 3))
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal())
    assert not outcome.filled and outcome.reason == "trading_disabled"
    t = _trade()
    assert t.status == "cancelled" and "trading_disabled" in (t.notes or "")


@pytest.mark.asyncio
async def test_ioc_no_fill_cancels_without_position():
    client = _FakeClient(resp={"order": {"order_id": "X", "fill_count": 0, "remaining_count": 3}})
    risk = _FakeRisk(TradeDecision(True, "Aprobado", 3))
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal())
    assert outcome.placed and not outcome.filled and outcome.reason == "ioc_no_fill"
    assert _trade().status == "cancelled"


@pytest.mark.asyncio
async def test_network_error_leaves_pending_uncertain_no_killswitch():
    from src.monitoring.health import BotState

    BotState.is_paused = False
    client = _FakeClient(exc=ConnectionError("red caída"))
    risk = _FakeRisk(TradeDecision(True, "Aprobado", 3))
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal())
    assert outcome.placed and not outcome.filled and outcome.uncertain
    t = _trade()
    # Fila queda 'pending' → el RiskManager la cuenta como exposición (conservador).
    assert t.status == "pending" and "error_red_unknown" in (t.notes or "")
    assert BotState.is_paused is False  # una apuesta incierta NO dispara kill-switch


@pytest.mark.asyncio
async def test_client_error_4xx_no_position():
    client = _FakeClient(exc=KalshiClientError(400, "precio inválido"))
    risk = _FakeRisk(TradeDecision(True, "Aprobado", 3))
    ex = Motor2Executor(client, risk)

    outcome = await ex.execute(_signal())
    assert outcome.placed and not outcome.filled
    assert _trade().status == "cancelled"


@pytest.mark.asyncio
async def test_zero_size_recommendation_skipped():
    client = _FakeClient(resp={"order": {}})
    ex = Motor2Executor(client, _FakeRisk(TradeDecision(True, "Aprobado", 5)))
    outcome = await ex.execute(_signal(size_usd=0.0))
    assert not outcome.placed
    client.place_order.assert_not_awaited()


class _FakeResolution:
    """SettlementSource falso: el mercado resolvió al lado dado."""

    def __init__(self, result: str):
        self._result = result

    async def get_resolution(self, ticker: str) -> MarketResolution:
        return MarketResolution(ticker=ticker, result=self._result)


@pytest.mark.asyncio
async def test_settlement_settles_single_leg_motor2_bet():
    """Una apuesta filled de Motor 2 se settlea (PnL realizado para el stop-loss)."""
    # Apostamos YES @ 40c; el mercado resuelve YES → ganadora: (100-40)*5 - fees.
    client = _FakeClient(resp={"order": {"order_id": "W", "fill_count": 5, "remaining_count": 0}})
    ex = Motor2Executor(client, _FakeRisk(TradeDecision(True, "Aprobado", 5)))
    await ex.execute(_signal(price=40, size_usd=2.0))
    assert _trade().status == "filled"

    settled = await SettlementPoller(_FakeResolution("yes")).settle_once()
    assert settled == 1
    t = _trade()
    assert t.status == "settled" and t.pnl_cents is not None and t.pnl_cents > 0  # ganó
