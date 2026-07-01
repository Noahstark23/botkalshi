"""
Deudas del executor de entrada de Motor 2 (auditoría 2026-07-01).

(a) Sizing: `max(1, round(...))` excedía el stake flat (round sube hasta medio contrato;
max(1,...) forzaba 1 contrato aunque el stake fuera menor que el precio → hasta 1.5× el
cap de sizing diseñado). Ahora: floor, y stake < 1 contrato → skip.
(b) Fill ilegible: HTTP 200 con fill_count null se trataba como 'cancelled' — si la orden
en realidad llenó, era exposición viva INVISIBLE al RiskManager (dirección de fallo
anti-conservadora). Ahora: fila queda 'pending' (patrón ERROR_RED) + error visible.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import src.storage.models as models
from src.risk.manager import TradeDecision
from src.strategies.motor_2_consensus.detector import ConsensusSignal
from src.strategies.motor_2_consensus.executor import Motor2Executor

TICKER = "KXWCGAME-26JUN27JORARG-ARG"


def _signal(*, size_usd: float, price: int) -> ConsensusSignal:
    return ConsensusSignal(
        market_ticker=TICKER,
        kalshi_side="YES",
        odds_api_fair_prob=0.62,
        kalshi_price_cents=price,
        edge_pct=0.05,
        recommended_size_usd=size_usd,
    )


class _FakeRisk:
    def __init__(self):
        self.last_opp = None

    async def check_pre_trade(self, opp):
        self.last_opp = opp
        return TradeDecision(True, "Aprobado", opp.count)


class _FakeClient:
    def __init__(self, resp):
        self.place_order = AsyncMock(return_value=resp)


def _trade() -> models.Trade | None:
    with models.get_session() as s:
        return s.exec(select(models.Trade).where(models.Trade.ticker == TICKER)).first()


@pytest.mark.asyncio
async def test_desired_count_floors_not_rounds():
    """$3.00 a 45c → 6.67 contratos: FLOOR = 6 (round daba 7 = $3.15, +5% del stake)."""
    client = _FakeClient({"order": {"order_id": "o", "fill_count": 6}})
    risk = _FakeRisk()
    ex = Motor2Executor(client, risk)

    out = await ex.execute(_signal(size_usd=3.00, price=45))

    assert out.filled
    assert risk.last_opp.count == 6


@pytest.mark.asyncio
async def test_stake_below_one_contract_skips():
    """$0.60 recomendado a 90c: max(1,...) forzaba 1 contrato = $0.90 (1.5× el stake).
    Ahora se saltea sin tocar red ni RiskManager."""
    client = _FakeClient({"order": {"order_id": "o", "fill_count": 1}})
    risk = _FakeRisk()
    ex = Motor2Executor(client, risk)

    out = await ex.execute(_signal(size_usd=0.60, price=90))

    assert not out.placed and out.reason == "stake_below_one_contract"
    client.place_order.assert_not_called()
    assert risk.last_opp is None


@pytest.mark.asyncio
async def test_fill_count_null_falls_back_to_fp_field():
    """fill_count presente con null NO enmascara fill_count_fp (coalesce por is-not-None)."""
    client = _FakeClient({"order": {"order_id": "o", "fill_count": None, "fill_count_fp": "5.00"}})
    ex = Motor2Executor(client, _FakeRisk())

    out = await ex.execute(_signal(size_usd=3.00, price=45))

    assert out.filled and out.filled_count == 5
    assert _trade().status == "filled"


@pytest.mark.asyncio
async def test_unreadable_fill_leaves_pending_not_cancelled():
    """HTTP 200 con fill ilegible en ambos campos → la fila queda 'pending' (el RiskManager
    la ve como exposición, conservador) en vez de 'cancelled' (posible posición viva
    invisible). Outcome incierto, sin kill-switch."""
    client = _FakeClient({"order": {"order_id": "o", "fill_count": None, "fill_count_fp": None}})
    ex = Motor2Executor(client, _FakeRisk())

    out = await ex.execute(_signal(size_usd=3.00, price=45))

    assert out.placed and not out.filled and out.uncertain
    assert out.reason == "fill_unreadable"
    t = _trade()
    assert t.status == "pending" and "fill_unreadable" in (t.notes or "")
