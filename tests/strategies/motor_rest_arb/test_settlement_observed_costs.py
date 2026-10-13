"""Regresión contable con dinero ficticio y SQLite temporal, sin API externa."""

from fractions import Fraction
from uuid import uuid4

import pytest
from sqlmodel import select

from src.storage.accounting import REALIZED_PNL, settled_pnl_cents
from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.ledger import record_order_fill
from src.strategies.motor_rest_arb.settlement import (
    MarketResolution,
    SettlementPoller,
    _leg_pnl_and_fees_cents,
)


class Resolution:
    def __init__(self, result="yes"):
        self.result = result

    async def get_resolution(self, ticker):
        return MarketResolution(ticker=ticker, result=self.result)


def observed_trade(**changes):
    row = Trade(
        client_order_id=str(uuid4()),
        ticker="TEST-ACCOUNTING",
        side="yes",
        action="buy",
        count=10,
        price_cents=50,
        strategy="motor_5_mm",
        status="filled",
    )
    record_order_fill(
        row,
        Fraction(10),
        {
            "fill_count_fp": "10.00",
            "maker_fees_dollars": "0.030000",
            "taker_fees_dollars": "0.000000",
            "maker_fill_cost_dollars": "4.090000",
            "taker_fill_cost_dollars": "0.000000",
        },
    )
    for key, value in changes.items():
        setattr(row, key, value)
    return row


def insert(*rows):
    with get_session() as session:
        session.add_all(rows)
        session.commit()


def read():
    with get_session() as session:
        return list(session.exec(select(Trade).order_by(Trade.id)))


@pytest.mark.parametrize("result,expected", [("yes", 588), ("no", -412)])
def test_actual_fill_ledger_uses_total_cost_instead_of_limit(result, expected):
    row = observed_trade()
    assert row.fill_price_cents is None  # Precio medio 40.9c, límite 50c.
    assert _leg_pnl_and_fees_cents(row, result) == (expected, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("result,pnl,cents", [("yes", "0.142350", 14), ("no", "-0.107650", -11)])
async def test_fractional_partial_fill_preserves_microdollars(result, pnl, cents):
    row = observed_trade(
        count=1,
        count_fp="1.00",
        filled_count=None,
        filled_count_fp="0.25",
        fill_cost_usd_fp="0.107525",
        fees_usd_fp="0.000125",
        fees_cents=None,
    )
    insert(row)
    assert await SettlementPoller(Resolution(result)).settle_once() == 1
    saved = read()[0]
    assert saved.pnl_usd_fp == pnl
    assert saved.pnl_cents == cents
    assert saved.fees_cents == 1
    assert saved.fees_usd_fp == "0.000125"
    assert "settlement_observed_costs" in saved.notes
    assert saved.accounting_role == REALIZED_PNL
    assert settled_pnl_cents(saved) == cents


@pytest.mark.asyncio
async def test_observed_and_legacy_group_settles_once_and_labels_each_basis():
    group = str(uuid4())
    observed = observed_trade(client_order_id=f"{group}-yes", notes=f"arb_id={group}")
    legacy = observed_trade(
        client_order_id=f"{group}-no",
        side="no",
        notes=f"arb_id={group}",
        fill_cost_usd_fp=None,
        fees_usd_fp=None,
    )
    insert(observed, legacy)
    assert await SettlementPoller(Resolution()).settle_once() == 2
    exact, estimated = read()
    assert exact.pnl_usd_fp == "5.8800"
    assert exact.pnl_cents == 588
    assert estimated.pnl_usd_fp == "-5.0300"
    assert "settlement_observed_costs" in exact.notes
    assert "settlement_estimated" in estimated.notes
    assert estimated.fill_cost_usd_fp is None and estimated.fees_usd_fp is None
    before = [row.model_dump() for row in read()]
    assert await SettlementPoller(Resolution()).settle_once() == 0
    assert [row.model_dump() for row in read()] == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("fill_cost_usd_fp", "NaN"),
        ("fill_cost_usd_fp", "-0.01"),
        ("fill_cost_usd_fp", "1e0"),
        ("fill_cost_usd_fp", " 4.090000"),
        ("fill_cost_usd_fp", "4.0900001"),
        ("fill_cost_usd_fp", "1000000000000000"),
        ("fill_cost_usd_fp", "10.000001"),
        ("fees_usd_fp", "Infinity"),
        ("fees_usd_fp", "-0.01"),
        ("fees_usd_fp", "10.000001"),
        ("fees_cents", -1),
        ("fees_cents", True),
        ("fees_cents", 4),
        ("side", "unknown"),
        ("action", "unknown"),
    ],
)
def test_invalid_observation_never_falls_back_to_estimate(field, value):
    row = observed_trade(**{field: value})
    with pytest.raises(ValueError):
        _leg_pnl_and_fees_cents(row, "yes")


@pytest.mark.parametrize("field", ["fill_cost_usd_fp", "fees_usd_fp"])
@pytest.mark.asyncio
async def test_bad_leg_rolls_back_whole_group(field):
    group = str(uuid4())
    good = observed_trade(client_order_id=f"{group}-yes")
    bad = observed_trade(client_order_id=f"{group}-no", **{field: "invalid"})
    insert(good, bad)
    with pytest.raises(ValueError):
        await SettlementPoller(Resolution()).settle_once()
    assert all(row.status == "filled" and row.pnl_usd_fp is None for row in read())
    assert all(row.pnl_cents is None and row.settled_at is None for row in read())


@pytest.mark.parametrize("fee", [None, "0.000000"])
@pytest.mark.asyncio
async def test_absent_fee_is_estimated_but_observed_zero_is_preserved(fee):
    insert(observed_trade(fees_usd_fp=fee, fees_cents=None))
    assert await SettlementPoller(Resolution()).settle_once() == 1
    saved = read()[0]
    if fee is None:
        assert saved.fees_cents > 0
        assert saved.pnl_cents < 591
        assert "settlement_estimated" in saved.notes
        assert saved.fees_usd_fp is None  # No fabricar una observación.
    else:
        assert saved.fees_cents == 0 and saved.pnl_cents == 591
        assert "settlement_observed_costs" in saved.notes


@pytest.mark.parametrize("result,expected", [("yes", "-5.000125"), ("no", "4.999875")])
@pytest.mark.asyncio
async def test_sell_known_fee_and_unknown_cost_is_estimated(result, expected):
    insert(
        observed_trade(
            action="sell",
            fill_cost_usd_fp=None,
            fees_usd_fp="0.000125",
            fees_cents=1,
        )
    )
    assert await SettlementPoller(Resolution(result)).settle_once() == 1
    saved = read()[0]
    assert saved.pnl_usd_fp == expected
    assert "settlement_estimated" in saved.notes
    assert saved.fill_cost_usd_fp is None
    assert saved.accounting_role == REALIZED_PNL
    assert settled_pnl_cents(saved) == saved.pnl_cents != 0


@pytest.mark.asyncio
async def test_contradictory_accounting_role_rolls_back_settlement():
    insert(observed_trade(accounting_role="exit_allocated"))
    before = read()[0].model_dump()
    with pytest.raises(ValueError, match="CONTRADICTORY_ACCOUNTING_ROLE"):
        await SettlementPoller(Resolution()).settle_once()
    assert read()[0].model_dump() == before


def test_sell_observed_cost_has_no_proven_semantics():
    with pytest.raises(ValueError, match="SELL"):
        _leg_pnl_and_fees_cents(observed_trade(action="sell"), "yes")


@pytest.mark.asyncio
async def test_marker_and_group_identity_survive_long_notes():
    group = str(uuid4())
    notes = "x" * 457 + f"arb_id={group}"
    assert len(notes) == 500
    insert(observed_trade(notes=notes))
    assert await SettlementPoller(Resolution()).settle_once() == 1
    saved = read()[0]
    assert len(saved.notes) <= 500
    assert "settlement_observed_costs" in saved.notes
    assert "settled_by_poller" in saved.notes
    assert f"arb_id={group}" in saved.notes


def test_zero_allocated_cost_and_fee_are_valid_observations():
    row = observed_trade(fill_cost_usd_fp="0.000000", fees_usd_fp="0.000000", fees_cents=0)
    assert _leg_pnl_and_fees_cents(row, "yes") == (1000, 0)


@pytest.mark.asyncio
async def test_group_changed_while_resolving_is_not_partially_settled():
    group = str(uuid4())
    insert(
        observed_trade(client_order_id=f"{group}-yes"),
        observed_trade(client_order_id=f"{group}-no"),
    )

    class ClosingSource(Resolution):
        async def get_resolution(self, ticker):
            with get_session() as session:
                row = session.exec(
                    select(Trade).where(Trade.client_order_id == f"{group}-no")
                ).one()
                row.closed_by_clv = True
                row.status = "settled"
                row.pnl_cents = 17
                session.add(row)
                session.commit()
            return await super().get_resolution(ticker)

    assert await SettlementPoller(ClosingSource()).settle_once() == 0
    yes, no = read()
    assert yes.status == "filled" and yes.pnl_cents is None
    assert no.status == "settled" and no.pnl_cents == 17
