"""Importes acumulados confirmados: subcentavos, ausencia y cancel parcial."""

from fractions import Fraction

import pytest
from sqlmodel import select

from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.strategies.motor_5_mm.ledger import confirmed_fill, record_order_fill
from src.strategies.motor_5_mm.quoter import QuoteSet
from src.strategies.motor_5_mm.reconciler import MMReconciler
from tests.strategies.motor_5_mm.test_engine_f2 import _LifecycleClient


def row(action="buy"):
    return Trade(
        client_order_id="cost-test",
        ticker="T",
        side="yes",
        action=action,
        count=1,
        price_cents=47,
        strategy="motor_5_mm",
        status="pending",
    )


def amounts(*, quantity="0.25", gross="0.107500", fees="0.000125"):
    return {
        "fill_count_fp": quantity,
        "maker_fill_cost_dollars": gross,
        "taker_fill_cost_dollars": "0.000000",
        "maker_fees_dollars": fees,
        "taker_fees_dollars": "0.000000",
    }


def test_buy_cost_and_subcent_fee_remain_exact():
    trade = row()
    record_order_fill(trade, Fraction(1, 4), amounts())
    assert trade.fill_cost_usd_fp == "0.107500"
    assert trade.fill_price_cents == 43
    assert trade.fees_usd_fp == "0.000125"
    assert trade.fees_cents == 1  # Cota legacy; el FP es la fuente exacta.
    assert confirmed_fill(trade) == Fraction(1, 4)


def test_subcent_average_never_fabricates_integer_fill_price():
    trade = row()
    record_order_fill(trade, Fraction(1, 4), amounts(gross="0.107525"))
    assert trade.fill_cost_usd_fp == "0.107525"
    assert trade.fill_price_cents is None


def test_sell_cost_is_not_assumed_to_be_confirmed_proceeds():
    trade = row("sell")
    record_order_fill(trade, Fraction(1, 4), amounts())
    assert trade.fill_cost_usd_fp is None
    assert trade.fill_price_cents is None
    assert trade.fees_usd_fp == "0.000125"


@pytest.mark.parametrize(
    "invalid", [None, True, 0.0, "NaN", "inf", "1e-4", "-0.1", "0.0000001", "1.00"]
)
def test_invalid_fee_does_not_invent_zero_or_drop_fill(invalid):
    trade = row()
    record_order_fill(trade, Fraction(1, 4), amounts(fees=invalid))
    assert trade.fees_usd_fp is None and trade.fees_cents is None
    assert confirmed_fill(trade) == Fraction(1, 4)
    assert "mm_cost_unverified" in trade.notes


def test_incomplete_pair_does_not_treat_missing_taker_as_zero():
    trade = row()
    data = amounts()
    del data["taker_fees_dollars"]
    record_order_fill(trade, Fraction(1, 4), data)
    assert trade.fees_usd_fp is None


def test_new_fill_cannot_inherit_previous_accumulated_costs():
    trade = row()
    record_order_fill(trade, Fraction(1, 4), amounts())
    record_order_fill(trade, Fraction(1, 2), {"fill_count_fp": "0.50"})
    assert confirmed_fill(trade) == Fraction(1, 2)
    assert trade.fill_cost_usd_fp is None
    assert trade.fees_usd_fp is None and trade.fees_cents is None
    assert trade.fill_price_cents is None


def test_regressive_fee_invalidates_cost_evidence_without_hiding_quantity():
    trade = row()
    record_order_fill(trade, Fraction(1, 4), amounts())
    record_order_fill(trade, Fraction(1, 2), amounts(quantity="0.50", gross="0.22", fees="0.0001"))
    assert confirmed_fill(trade) == Fraction(1, 2)
    assert trade.fees_usd_fp is None and trade.fees_cents is None


@pytest.mark.asyncio
async def test_reconciler_persists_resting_fractional_fill_and_money_together():
    client = _LifecycleClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(
        QuoteSet(ticker="T", fair_prob=0.5, bid_cents=47, ask_cents=None, size=1)
    )
    client.orders[0].update(amounts())
    report = await MMReconciler(client).reconcile(live_coids=executor.live_coids())
    assert report.complete and report.still_resting == 1
    with get_session() as session:
        trade = session.exec(select(Trade)).one()
        assert trade.status == "pending"
        assert trade.fill_cost_usd_fp == "0.107500"
        assert trade.fees_usd_fp == "0.000125"
        assert confirmed_fill(trade) == Fraction(1, 4)


@pytest.mark.asyncio
async def test_executor_cancel_keeps_confirmed_fractional_money():
    client = _LifecycleClient()
    client.cancel_fails = False
    executor = Motor5Executor(client)
    await executor.sync_quotes(
        QuoteSet(ticker="T", fair_prob=0.5, bid_cents=47, ask_cents=None, size=1)
    )
    client.orders[0].update(amounts(), remaining_count_fp="0.00")
    await executor.retire_ticker("T")
    assert not executor.live_coids()
    with get_session() as session:
        trade = session.exec(select(Trade)).one()
        assert trade.status == "filled"
        assert trade.fill_cost_usd_fp == "0.107500"
        assert trade.fees_usd_fp == "0.000125"
        assert confirmed_fill(trade) == Fraction(1, 4)


@pytest.mark.asyncio
async def test_create_ack_preserves_explicit_zero_but_never_invents_it():
    client = _LifecycleClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(
        QuoteSet(ticker="T", fair_prob=0.5, bid_cents=47, ask_cents=None, size=1)
    )
    with get_session() as session:
        trade = session.exec(select(Trade)).one()
        assert confirmed_fill(trade) == 0
        assert trade.fees_usd_fp is None  # no fill, no comisión observada

    original = client.place_order

    async def ack_without_quantity(**kwargs):
        response = await original(**kwargs)
        del response["order"]["fill_count_fp"]
        return response

    client.place_order = ack_without_quantity
    await executor.sync_quotes(
        QuoteSet(ticker="U", fair_prob=0.5, bid_cents=47, ask_cents=None, size=1)
    )
    with get_session() as session:
        trade = session.exec(select(Trade).where(Trade.ticker == "U")).one()
        assert confirmed_fill(trade) is None


@pytest.mark.asyncio
async def test_partial_fill_on_create_preserves_cost_and_waits_before_other_side():
    client = _LifecycleClient()
    original = client.place_order

    async def partial_on_create(**kwargs):
        response = await original(**kwargs)
        response["order"].update(amounts())
        client.orders[-1].update(amounts())
        return response

    client.place_order = partial_on_create
    executor = Motor5Executor(client)
    outcome = await executor.sync_quotes(
        QuoteSet(ticker="T", fair_prob=0.5, bid_cents=47, ask_cents=53, size=1)
    )
    assert outcome == "risk_blocked"
    assert len(client.orders) == 1
    with get_session() as session:
        trade = session.exec(select(Trade)).one()
        assert trade.status == "pending"
        assert confirmed_fill(trade) == Fraction(1, 4)
        assert trade.fill_cost_usd_fp == "0.107500"
        assert trade.fees_usd_fp == "0.000125"
