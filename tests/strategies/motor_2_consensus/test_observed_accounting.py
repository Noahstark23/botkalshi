"""Importes M2 en ACK y GET ficticios: nunca se consulta un exchange."""

from fractions import Fraction
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
from src.math.quantities import fixed_count
from src.monitoring.health import BotState
from src.risk.exposure import open_risk_cents
from src.risk.manager import TradeDecision
from src.storage.order_costs import COST_QUARANTINE_NOTE, record_order_fill
from src.storage.order_reconciliation import reconcile_pending_orders
from src.storage.quantities import confirmed_fill
from src.strategies.motor_2_consensus.executor import Motor2Executor
from src.strategies.motor_rest_arb.settlement import (
    MarketResolution,
    SettlementPoller,
    _leg_pnl_cents,
)
from tests.strategies.motor_2_consensus.test_executor import _FakeClient, _FakeRisk, _signal, _trade


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    denied = Mock(side_effect=AssertionError("ONLY_FICTITIOUS_RESPONSES"))
    monkeypatch.setattr("socket.socket.connect", denied)
    monkeypatch.setattr("socket.create_connection", denied)
    monkeypatch.setattr("src.clients.kalshi_rest.KalshiRestClient.__init__", denied)
    yield
    denied.assert_not_called()


def order(*, fill="0.25", gross="0.107525", fee="0.000125", status="canceled"):
    return {
        "order_id": "fictional-order",
        "status": status,
        "fill_count_fp": fill,
        "initial_count_fp": "1.00",
        "remaining_count_fp": fixed_count(1 - Fraction(fill)) if status == "resting" else "0.00",
        "maker_fill_cost_dollars": gross,
        "taker_fill_cost_dollars": "0.000000",
        "maker_fees_dollars": fee,
        "taker_fees_dollars": "0.000000",
    }


async def execute(data):
    client = _FakeClient(resp={"order": data})
    outcome = await Motor2Executor(client, _FakeRisk(TradeDecision(True, "fixture", 1))).execute(
        _signal(price=50, size_usd=0.5)
    )
    return client, outcome


class Resolved:
    def __init__(self, result="yes"):
        self.result = result

    async def get_resolution(self, ticker):
        return MarketResolution(ticker, self.result)


@pytest.mark.parametrize("result,pnl,cents", [("yes", "0.142350", 14), ("no", "-0.107650", -11)])
async def test_ack_to_settlement_conserves_actual_totals(result, pnl, cents):
    client, outcome = await execute(order())
    assert outcome.filled and outcome.filled_count == Fraction(1, 4)
    row = _trade()
    assert (row.fill_cost_usd_fp, row.fees_usd_fp) == ("0.107525", "0.000125")
    assert row.fill_price_cents is None and row.fees_cents == 1
    assert row.count_fp == "0.25" and confirmed_fill(row) == Fraction(1, 4)
    assert row.observed_cost_floor_usd_fp == "0.107525"
    assert row.observed_cost_floor_count_fp == "0.25"
    assert row.observed_fee_floor_usd_fp == "0.000125"
    assert row.observed_fee_floor_count_fp == "0.25"
    assert await SettlementPoller(Resolved(result)).settle_once() == 1
    row = _trade()
    assert row.pnl_usd_fp == pnl and row.pnl_cents == cents
    assert "settlement_observed_costs" in row.notes
    saved = row.model_dump()
    assert await SettlementPoller(Resolved(result)).settle_once() == 0
    assert _trade().model_dump() == saved
    client.place_order.assert_awaited_once()


async def test_uncertain_ack_then_get_and_new_connection_preserve_money(monkeypatch):
    client, outcome = await execute(order(status="resting"))
    assert outcome.uncertain and BotState.is_paused
    row = _trade()
    assert row.status == "pending" and row.count_fp == "1.00"
    assert row.fill_cost_usd_fp == "0.107525" and row.fees_usd_fp == "0.000125"
    assert open_risk_cents(row) == 50
    old = models._engine
    url = old.url
    old.dispose()
    reopened = create_engine(url)
    monkeypatch.setattr(models, "_engine", reopened)
    try:
        observation = order(fill="0.50", gross="0.210050", fee="0.000250")
        observation["client_order_id"] = row.client_order_id
        reader = AsyncMock()
        reader.get_orders.return_value = {"orders": [observation]}
        report = await reconcile_pending_orders(reader, strategies=("motor_2_consensus",))
        assert report.resolved == 1 and not report.unresolved
        row = _trade()
        assert row.status == "filled" and row.count_fp == "0.50"
        assert (row.fill_cost_usd_fp, row.fees_usd_fp) == ("0.210050", "0.000250")
        assert open_risk_cents(row) == 25
        assert await SettlementPoller(Resolved()).settle_once() == 1
        assert _trade().pnl_usd_fp == "0.2897"
        assert (
            await reconcile_pending_orders(reader, strategies=("motor_2_consensus",))
        ).checked == 0
        reader.place_order.assert_not_called()
        reader.cancel_order.assert_not_called()
        client.place_order.assert_awaited_once()
        assert BotState.is_paused  # Contabilizar no autoriza levantar una pausa.
    finally:
        reopened.dispose()


@pytest.mark.parametrize("field", ["client_order_id", "ticker", "side", "action"])
async def test_ack_identity_conflict_cannot_attribute_fill_or_cost(field):
    data = order()
    data[field] = "unrelated"
    _, outcome = await execute(data)
    assert outcome.uncertain and outcome.reason == "order_identity_unknown"
    row = _trade()
    assert row.status == "pending" and row.count_fp == "1.00"
    assert confirmed_fill(row) is None and row.fill_cost_usd_fp is None
    assert BotState.is_paused and open_risk_cents(row) == 50


async def test_invalid_money_keeps_quantity_and_original_reservation():
    _, outcome = await execute(order(fee="NaN"))
    assert outcome.uncertain and outcome.reason == "fill_accounting_unknown"
    row = _trade()
    assert row.status == "pending" and row.count_fp == "1.00"
    assert confirmed_fill(row) == Fraction(1, 4)
    assert COST_QUARANTINE_NOTE in row.notes and row.fees_usd_fp is None
    assert BotState.is_paused and open_risk_cents(row) == 50
    observation = order()  # Ni un replay posterior válido levanta esta contradicción.
    observation["client_order_id"] = row.client_order_id
    reader = AsyncMock()
    reader.get_orders.return_value = {"orders": [observation]}
    assert (await reconcile_pending_orders(reader, strategies=("motor_2_consensus",))).unresolved
    assert _trade().status == "pending" and _trade().fill_cost_usd_fp is None
    assert await SettlementPoller(Resolved()).settle_once() == 0


async def test_regressive_money_replay_cannot_rehabilitate_observed_cost():
    await execute(order(status="resting"))
    row = _trade()
    data = order(fill="0.50", gross="0.080000", fee="0.000250")
    data["client_order_id"] = row.client_order_id
    reader = AsyncMock()
    reader.get_orders.return_value = {"orders": [data]}
    for _ in range(2):
        result = await reconcile_pending_orders(reader, strategies=("motor_2_consensus",))
        assert result.unresolved and result.resolved == 0
        row = _trade()
        assert row.status == "pending" and row.count_fp == "1.00"
        assert confirmed_fill(row) == Fraction(1, 2)
        assert row.fill_cost_usd_fp is None and COST_QUARANTINE_NOTE in row.notes
        assert open_risk_cents(row) == 50


async def test_new_fill_without_totals_cannot_reuse_previous_money():
    await execute(order(status="resting"))
    row = _trade()
    reader = AsyncMock()
    reader.get_orders.return_value = {
        "orders": [
            {
                "client_order_id": row.client_order_id,
                "order_id": row.kalshi_order_id,
                "status": "canceled",
                "fill_count_fp": "0.50",
                "remaining_count_fp": "0.00",
            }
        ]
    }
    assert (await reconcile_pending_orders(reader, strategies=("motor_2_consensus",))).resolved == 1
    row = _trade()
    assert row.fill_cost_usd_fp is None and row.fees_usd_fp is None
    assert row.fees_cents is None and row.fill_price_cents is None
    assert await SettlementPoller(Resolved()).settle_once() == 1
    assert "settlement_estimated" in _trade().notes


async def test_get_rechecks_order_identity_after_await():
    await execute(order(status="resting"))
    original = _trade()

    async def changing_get(**kwargs):
        with models.get_session() as session:
            row = session.exec(select(models.Trade)).one()
            row.kalshi_order_id = "changed-while-awaiting"
            session.add(row)
            session.commit()
        data = order(fill="0.50", gross="0.210050", fee="0.000250")
        data["client_order_id"] = original.client_order_id
        return {"orders": [data]}

    reader = AsyncMock()
    reader.get_orders.side_effect = changing_get
    result = await reconcile_pending_orders(reader, strategies=("motor_2_consensus",))
    assert result.unresolved
    row = _trade()
    assert row.status == "pending" and confirmed_fill(row) == Fraction(1, 4)
    assert row.fill_cost_usd_fp == "0.107525"
    assert row.kalshi_order_id == "changed-while-awaiting"


def test_settlement_rejects_durable_cost_quarantine_even_with_valid_looking_totals():
    row = models.Trade(
        client_order_id="quarantine-test",
        ticker="T",
        side="yes",
        action="buy",
        count=1,
        filled_count=1,
        price_cents=50,
        strategy="motor_5_mm",
        status="filled",
        fill_cost_usd_fp="0.40",
        fees_usd_fp="0.00",
        fees_cents=0,
        notes=COST_QUARANTINE_NOTE,
    )
    with pytest.raises(ValueError, match="cuarentena"):
        _leg_pnl_cents(row, "yes")


@pytest.mark.parametrize("fee", ["NaN", "0.010000"])
async def test_zero_fill_ack_with_invalid_money_cannot_cancel_cleanly(fee):
    _, outcome = await execute(order(fill="0.00", gross="0.000000", fee=fee))
    assert outcome.uncertain and outcome.reason == "fill_accounting_unknown"
    row = _trade()
    assert row.status == "pending" and confirmed_fill(row) == 0
    assert COST_QUARANTINE_NOTE in row.notes
    assert open_risk_cents(row) == 50 and BotState.is_paused


async def test_late_zero_ack_cannot_erase_fill_persisted_while_post_was_awaited():
    async def response_after_recovery(**kwargs):
        with models.get_session() as session:
            row = session.exec(select(models.Trade)).one()
            record_order_fill(row, Fraction(1, 4), order(status="resting"))
            row.kalshi_order_id = "fictional-order"
            session.add(row)
            session.commit()
        return {"order": order(fill="0.00", gross="0.000000", fee="0.000000")}

    client = _FakeClient()
    client.place_order.side_effect = response_after_recovery
    outcome = await Motor2Executor(client, _FakeRisk(TradeDecision(True, "fixture", 1))).execute(
        _signal(price=50, size_usd=0.5)
    )
    assert outcome.uncertain and outcome.reason == "fill_accounting_unknown"
    row = _trade()
    assert row.status == "pending" and confirmed_fill(row) == Fraction(1, 4)
    assert row.fill_cost_usd_fp == "0.107525" and row.fees_usd_fp == "0.000125"
    assert open_risk_cents(row) == 50 and BotState.is_paused
    client.place_order.assert_awaited_once()
