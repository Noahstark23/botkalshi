"""Dinero ficticio: acumulados exactos y cuarentena durable, sin red ni decisiones."""

from fractions import Fraction

import pytest
from sqlmodel import Session, create_engine, select

from src.math.fees import kalshi_fee_cents
from src.storage.models import SQLModel, Trade
from src.storage.order_costs import COST_QUARANTINE_NOTE, record_order_fill
from src.storage.quantities import confirmed_fill, requested_quantity


def trade(**changes):
    row = Trade(
        client_order_id="cost-test",
        ticker="TEST-ACCOUNTING",
        side="yes",
        action="buy",
        strategy="motor_2_consensus",
        count=1,
        count_fp="1.00",
        price_cents=47,
        status="pending",
    )
    for field, value in changes.items():
        setattr(row, field, value)
    return row


def observation(quantity="0.25", gross="0.107525", fee="0.000125"):
    return {
        "fill_count_fp": quantity,
        "maker_fill_cost_dollars": gross,
        "taker_fill_cost_dollars": "0.000000",
        "maker_fees_dollars": fee,
        "taker_fees_dollars": "0.000000",
    }


def assert_cleared(row):
    assert row.fill_cost_usd_fp is None
    assert row.fees_usd_fp is None
    assert row.fill_price_cents is None
    assert row.fees_cents is None


def test_exact_totals_subcent_fee_and_reexport_compatibility():
    from src.strategies.motor_5_mm import ledger

    assert ledger.record_order_fill is record_order_fill
    assert ledger.confirmed_fill is confirmed_fill
    row = trade()
    record_order_fill(row, Fraction(1, 4), observation())
    assert confirmed_fill(row) == Fraction(1, 4)
    assert requested_quantity(row) == 1 and row.status == "pending"
    assert row.fill_cost_usd_fp == "0.107525"
    assert row.fees_usd_fp == "0.000125" and row.fees_cents == 1
    assert row.fill_price_cents is None  # Promedio43.01c no se trunca a43c.
    before = row.model_dump()
    record_order_fill(row, Fraction(1, 4), observation())
    assert row.model_dump() == before


def test_integer_average_and_observed_zero_fee_are_preserved():
    row = trade()
    record_order_fill(row, Fraction(1, 4), observation(gross="0.107500", fee="0.000000"))
    assert row.fill_price_cents == 43
    assert row.fees_usd_fp == "0.000000" and row.fees_cents == 0
    assert COST_QUARANTINE_NOTE not in (row.notes or "")


def test_fee_and_gross_sum_both_complete_components():
    row = trade()
    data = observation(gross="0.100000", fee="0.000100")
    data.update(taker_fill_cost_dollars="0.007525", taker_fees_dollars="0.000025")
    record_order_fill(row, Fraction(1, 4), data)
    assert row.fill_cost_usd_fp == "0.107525" and row.fees_usd_fp == "0.000125"


@pytest.mark.parametrize("field", ["maker_fees_dollars", "maker_fill_cost_dollars"])
@pytest.mark.parametrize(
    "invalid", [True, 0, 0.1, "NaN", "inf", "-0.1", "1e-4", "0.0000001", " 0.1", "١.٠", "1.00"]
)
def test_invalid_present_money_quarantines_without_hiding_fill(field, invalid):
    row = trade(notes="x" * 500)
    data = observation()
    data[field] = invalid
    record_order_fill(row, Fraction(1, 4), data)
    assert confirmed_fill(row) == Fraction(1, 4)
    assert requested_quantity(row) == 1 and row.status == "pending"
    assert_cleared(row)
    assert row.notes.startswith(COST_QUARANTINE_NOTE) and len(row.notes) <= 500
    record_order_fill(row, Fraction(1, 4), observation())
    assert_cleared(row)
    assert row.notes.startswith(COST_QUARANTINE_NOTE)


@pytest.mark.parametrize("remove", [False, True])
def test_missing_component_is_unknown_not_zero_or_quarantine(remove):
    row = trade()
    data = observation()
    if remove:
        del data["taker_fees_dollars"]
    else:
        data["taker_fees_dollars"] = None
    record_order_fill(row, Fraction(1, 4), data)
    assert row.fees_usd_fp is None and row.fees_cents is None
    assert row.fill_cost_usd_fp == "0.107525"
    assert COST_QUARANTINE_NOTE not in row.notes


def test_invalid_component_is_not_hidden_by_absent_pair():
    row = trade()
    data = {"fill_count_fp": "0.25", "maker_fees_dollars": "invalid"}
    record_order_fill(row, Fraction(1, 4), data)
    assert row.notes.startswith(COST_QUARANTINE_NOTE)
    assert_cleared(row)


def test_new_quantity_never_inherits_previous_costs_or_legacy_estimate():
    row = trade()
    record_order_fill(row, Fraction(1, 4), observation())
    record_order_fill(row, Fraction(1, 2), {"fill_count_fp": "0.50"}, legacy_estimates=True)
    assert confirmed_fill(row) == Fraction(1, 2)
    assert_cleared(row)
    assert COST_QUARANTINE_NOTE not in row.notes


@pytest.mark.parametrize("field", ["gross", "fee"])
def test_regression_cannot_rehabilitate_money_on_repeated_observation(field):
    row = trade()
    record_order_fill(row, Fraction(1, 4), observation(gross="0.100000", fee="0.001000"))
    data = observation(
        "0.50",
        gross="0.080000" if field == "gross" else "0.200000",
        fee="0.000800" if field == "fee" else "0.002000",
    )
    for _ in range(2):
        record_order_fill(row, Fraction(1, 2), data)
        assert confirmed_fill(row) == Fraction(1, 2)
        assert_cleared(row)
        assert row.notes.startswith(COST_QUARANTINE_NOTE)
    record_order_fill(row, Fraction(3, 4), observation("0.75", "0.300000", "0.003000"))
    assert confirmed_fill(row) == Fraction(3, 4)
    assert_cleared(row)


def test_same_quantity_changed_money_is_contradictory():
    row = trade()
    record_order_fill(row, Fraction(1, 4), observation())
    record_order_fill(row, Fraction(1, 4), observation(fee="0.000126"))
    assert_cleared(row)
    assert row.notes.startswith(COST_QUARANTINE_NOTE)


@pytest.mark.parametrize("quantity", [Fraction(1, 10), Fraction(2)])
def test_incompatible_fill_never_overwrites_quantity_or_money(quantity):
    row = trade()
    record_order_fill(row, Fraction(1, 4), observation())
    before = row.model_dump()
    with pytest.raises(ValueError):
        record_order_fill(row, quantity, observation())
    assert row.model_dump() == before


def test_response_for_other_quantity_never_attaches_its_money():
    row = trade()
    record_order_fill(row, Fraction(1, 4), observation())
    before = row.model_dump()
    record_order_fill(row, Fraction(1, 4), observation("0.50"))
    assert row.model_dump() == before  # Compat: replay no correlacionado se ignora.
    record_order_fill(row, Fraction(1, 2), observation("0.25"), legacy_estimates=True)
    assert confirmed_fill(row) == Fraction(1, 2)
    assert_cleared(row)


def test_zero_fill_with_zero_money_is_not_quarantined():
    row = trade()
    record_order_fill(row, 0, observation("0.00", "0.000000", "0.000000"))
    assert confirmed_fill(row) == 0
    assert_cleared(row)
    assert COST_QUARANTINE_NOTE not in (row.notes or "")


def test_positive_money_on_zero_fill_is_quarantined():
    row = trade()
    record_order_fill(row, 0, observation("0.00"))
    assert confirmed_fill(row) == 0
    assert_cleared(row)
    assert row.notes.startswith(COST_QUARANTINE_NOTE)


def test_sell_keeps_exact_fee_but_never_treats_cost_as_proceeds():
    row = trade(action="sell")
    record_order_fill(row, Fraction(1, 4), observation())
    assert row.fees_usd_fp == "0.000125" and row.fees_cents == 1
    assert row.fill_cost_usd_fp is None and row.fill_price_cents is None


@pytest.mark.parametrize("action,enabled", [("buy", True), ("buy", False), ("sell", True)])
def test_legacy_estimates_are_explicit_buy_only_without_money_fields(action, enabled):
    row = trade(action=action)
    record_order_fill(row, Fraction(1, 4), {"fill_count_fp": "0.25"}, legacy_estimates=enabled)
    assert row.fill_cost_usd_fp is None and row.fees_usd_fp is None
    if action == "buy" and enabled:
        assert row.fill_price_cents == 47
        assert row.fees_cents == kalshi_fee_cents(Fraction(1, 4), 47)
    else:
        assert_cleared(row)


@pytest.mark.parametrize(
    "field",
    [
        "maker_fees_dollars",
        "taker_fees_dollars",
        "maker_fill_cost_dollars",
        "taker_fill_cost_dollars",
    ],
)
def test_null_money_key_disables_legacy_estimate(field):
    row = trade()
    record_order_fill(
        row, Fraction(1, 4), {"fill_count_fp": "0.25", field: None}, legacy_estimates=True
    )
    assert_cleared(row)
    assert COST_QUARANTINE_NOTE not in row.notes


@pytest.mark.parametrize("price", [True, 0, 100, 47.5])
def test_legacy_estimate_requires_valid_integer_price(price):
    row = trade(price_cents=price)
    record_order_fill(row, Fraction(1, 4), {"fill_count_fp": "0.25"}, legacy_estimates=True)
    assert_cleared(row)


def test_quarantine_survives_sqlite_reopen_and_legacy_fallback(tmp_path):
    path = f"sqlite:///{tmp_path}/ledger.db"
    engine = create_engine(path)
    SQLModel.metadata.create_all(engine)
    row = trade()
    record_order_fill(row, Fraction(1, 4), observation(gross="0.100000"))
    record_order_fill(row, Fraction(1, 2), observation("0.50", "0.080000"))
    with Session(engine) as session:
        session.add(row)
        session.commit()
    engine.dispose()
    engine = create_engine(path)
    with Session(engine) as session:
        loaded = session.exec(select(Trade)).one()
        record_order_fill(loaded, Fraction(1, 2), observation("0.50", "0.080000"))
        record_order_fill(loaded, Fraction(1, 2), {"fill_count_fp": "0.50"}, legacy_estimates=True)
        assert_cleared(loaded)
        assert confirmed_fill(loaded) == Fraction(1, 2)
        assert loaded.notes.startswith(COST_QUARANTINE_NOTE)
        session.add(loaded)
        session.commit()
    engine.dispose()
