"""Historia monetaria sobre dinero ficticio: ausencias, reinicio y splits contables."""

from fractions import Fraction

import pytest
from sqlmodel import Session, create_engine, select

from src.storage.models import SQLModel, Trade
from src.storage.order_costs import COST_QUARANTINE_NOTE, record_order_fill
from src.storage.quantities import confirmed_fill, record_fill, requested_quantity, resize_filled

FLOOR_FIELDS = (
    "observed_cost_floor_usd_fp",
    "observed_cost_floor_count_fp",
    "observed_fee_floor_usd_fp",
    "observed_fee_floor_count_fp",
)


def row(**changes):
    result = Trade(
        client_order_id="history-test",
        ticker="TEST-HISTORY",
        side="yes",
        action="buy",
        strategy="motor_2_consensus",
        count=1,
        count_fp="1.00",
        price_cents=47,
        status="pending",
    )
    for field, value in changes.items():
        setattr(result, field, value)
    return result


def observation(quantity="0.25", *, gross="0.100000", fee="0.001000"):
    result = {"fill_count_fp": quantity}
    if gross is not None:
        result.update(maker_fill_cost_dollars=gross, taker_fill_cost_dollars="0.000000")
    if fee is not None:
        result.update(maker_fees_dollars=fee, taker_fees_dollars="0.000000")
    return result


def floors(trade):
    return tuple(getattr(trade, name) for name in FLOOR_FIELDS)


def cleared_current(trade):
    assert trade.fill_cost_usd_fp is None and trade.fees_usd_fp is None
    assert trade.fill_price_cents is None and trade.fees_cents is None


def initialized():
    trade = row()
    record_order_fill(trade, Fraction(1, 4), observation())
    return trade


@pytest.mark.parametrize("gross,fee", [("0.080000", "0.002000"), ("0.200000", "0.000800")])
def test_missing_gap_cannot_hide_later_regression(gross, fee):
    trade = initialized()
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=None, fee=None))
    cleared_current(trade)
    assert floors(trade) == ("0.100000", "0.25", "0.001000", "0.25")
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=gross, fee=fee))
    assert confirmed_fill(trade) == Fraction(1, 2)
    assert requested_quantity(trade) == 1
    assert COST_QUARANTINE_NOTE in trade.notes
    cleared_current(trade)
    # Un payload contradictorio no reemplaza los pisos conocidos con su dinero.
    assert floors(trade) == ("0.100000", "0.25", "0.001000", "0.25")


def test_valid_recovery_after_gap_advances_both_histories():
    trade = initialized()
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=None, fee=None))
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross="0.200000", fee="0.002000"))
    assert floors(trade) == ("0.200000", "0.50", "0.002000", "0.50")
    assert trade.fill_cost_usd_fp == "0.200000" and trade.fees_usd_fp == "0.002000"
    assert COST_QUARANTINE_NOTE not in (trade.notes or "")


@pytest.mark.parametrize("gross,fee", [("0.100001", "0.001000"), ("0.100000", "0.001001")])
def test_equal_quantity_requires_equal_money_after_gap(gross, fee):
    trade = initialized()
    record_order_fill(trade, Fraction(1, 4), observation(gross=None, fee=None))
    record_order_fill(trade, Fraction(1, 4), observation(gross=gross, fee=fee))
    assert COST_QUARANTINE_NOTE in trade.notes
    cleared_current(trade)
    assert floors(trade) == ("0.100000", "0.25", "0.001000", "0.25")


def test_cost_and_fee_histories_have_independent_quantities():
    trade = initialized()
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=None, fee="0.002000"))
    assert trade.fill_cost_usd_fp is None
    assert floors(trade) == ("0.100000", "0.25", "0.002000", "0.50")
    record_order_fill(trade, Fraction(3, 4), observation("0.75", gross="0.300000", fee=None))
    assert trade.fees_usd_fp is None
    assert floors(trade) == ("0.300000", "0.75", "0.002000", "0.50")
    record_order_fill(trade, Fraction(3, 4), observation("0.75", gross="0.300000", fee="0.003000"))
    assert floors(trade) == ("0.300000", "0.75", "0.003000", "0.75")
    assert COST_QUARANTINE_NOTE not in (trade.notes or "")


def test_zero_observed_fee_is_a_real_history_not_absence():
    trade = row()
    record_order_fill(trade, Fraction(1, 4), observation(fee="0.000000"))
    record_order_fill(trade, Fraction(1, 4), observation(gross=None, fee=None))
    assert floors(trade)[2:] == ("0.000000", "0.25")
    record_order_fill(trade, Fraction(1, 4), observation(fee="0.000001"))
    assert COST_QUARANTINE_NOTE in trade.notes


def test_legacy_current_fp_seeds_history_before_quantity_changes():
    trade = row(fill_cost_usd_fp="0.107525", fees_usd_fp="0.000125", fees_cents=1)
    record_fill(trade, Fraction(1, 4))
    assert floors(trade) == (None, None, None, None)
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=None, fee=None))
    cleared_current(trade)
    assert floors(trade) == ("0.107525", "0.25", "0.000125", "0.25")


def test_legacy_fp_without_confirmed_quantity_cannot_seed_from_intent():
    trade = row(fill_cost_usd_fp="0.107525", fees_usd_fp="0.000125")
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=None, fee=None))
    assert confirmed_fill(trade) == Fraction(1, 2)
    assert COST_QUARANTINE_NOTE in trade.notes
    assert floors(trade) == (None, None, None, None)
    cleared_current(trade)


@pytest.mark.parametrize(
    "amount,quantity",
    [
        ("0.100000", None),
        (None, "0.25"),
        ("NaN", "0.25"),
        ("-0.1", "0.25"),
        ("0.1000001", "0.25"),
        ("0.100000", "0.001"),
        ("0.100000", "invalid"),
        ("0.100000", "0.75"),
        ("0.300000", "0.25"),
    ],
)
@pytest.mark.parametrize("kind", ["cost", "fee"])
def test_malformed_or_incomplete_history_quarantines_without_erasing_evidence(
    amount, quantity, kind
):
    trade = row()
    record_fill(trade, Fraction(1, 2))
    setattr(trade, f"observed_{kind}_floor_usd_fp", amount)
    setattr(trade, f"observed_{kind}_floor_count_fp", quantity)
    before = floors(trade)
    record_order_fill(trade, Fraction(3, 4), observation("0.75", gross="0.300000", fee="0.003000"))
    assert confirmed_fill(trade) == Fraction(3, 4)
    assert COST_QUARANTINE_NOTE in trade.notes
    assert floors(trade) == before
    cleared_current(trade)


def test_repeated_missing_payloads_never_reenable_legacy_estimates():
    trade = initialized()
    for quantity in ("0.50", "0.50", "0.75"):
        record_order_fill(
            trade,
            Fraction(quantity),
            observation(quantity, gross=None, fee=None),
            legacy_estimates=True,
        )
        cleared_current(trade)
        assert floors(trade) == ("0.100000", "0.25", "0.001000", "0.25")


def test_quarantine_replay_keeps_old_floors_and_never_restores_current_money():
    trade = initialized()
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross="0.080000"))
    before = floors(trade)
    for quantity in ("0.50", "0.75"):
        record_order_fill(
            trade, Fraction(quantity), observation(quantity, gross="0.300000", fee="0.003000")
        )
        assert COST_QUARANTINE_NOTE in trade.notes
        assert floors(trade) == before
        cleared_current(trade)
    assert confirmed_fill(trade) == Fraction(3, 4)


def test_sqlite_reopen_preserves_floor_even_without_current_totals(tmp_path):
    url = f"sqlite:///{tmp_path}/history.db"
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
    trade = initialized()
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=None, fee=None))
    with Session(engine) as session:
        session.add(trade)
        session.commit()
    engine.dispose()
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            persisted = session.exec(select(Trade)).one()
            assert floors(persisted) == ("0.100000", "0.25", "0.001000", "0.25")
            cleared_current(persisted)
            record_order_fill(persisted, Fraction(1, 2), observation("0.50", gross="0.080000"))
            session.add(persisted)
            session.commit()
        with Session(engine) as session:
            persisted = session.exec(select(Trade)).one()
            assert COST_QUARANTINE_NOTE in persisted.notes
            assert floors(persisted) == ("0.100000", "0.25", "0.001000", "0.25")
            cleared_current(persisted)
    finally:
        engine.dispose()


def test_terminal_resize_to_same_confirmed_fill_preserves_history():
    trade = initialized()
    resize_filled(trade, Fraction(1, 4))
    assert requested_quantity(trade) == Fraction(1, 4)
    assert floors(trade) == ("0.100000", "0.25", "0.001000", "0.25")


def test_split_reduction_clears_order_history_but_preserves_quarantine_marker():
    trade = initialized()
    trade.notes = COST_QUARANTINE_NOTE
    resize_filled(trade, Fraction(1, 10))
    assert confirmed_fill(trade) == Fraction(1, 10)
    assert floors(trade) == (None, None, None, None)
    assert COST_QUARANTINE_NOTE in trade.notes


@pytest.mark.parametrize("quantity", [Fraction(-1, 100), Fraction(1, 1000)])
def test_invalid_resize_leaves_quantity_money_and_floors_unchanged(quantity):
    trade = initialized()
    before = trade.model_dump()
    with pytest.raises(ValueError):
        resize_filled(trade, quantity)
    assert trade.model_dump() == before


@pytest.mark.parametrize("quantity", [Fraction(1, 10), Fraction(2)])
def test_invalid_record_quantity_cannot_partially_seed_legacy_floors(quantity):
    trade = row(fill_cost_usd_fp="0.107525", fees_usd_fp="0.000125", fees_cents=1)
    record_fill(trade, Fraction(1, 4))
    before = trade.model_dump()
    with pytest.raises(ValueError):
        record_order_fill(trade, quantity, observation(str(float(quantity))))
    assert trade.model_dump() == before


def test_sell_only_persists_fee_history_not_ambiguous_gross():
    trade = row(action="sell")
    record_order_fill(trade, Fraction(1, 4), observation())
    assert floors(trade) == (None, None, "0.001000", "0.25")
    assert trade.fill_cost_usd_fp is None


@pytest.mark.parametrize("legacy_fee", [None, 0, 1])
def test_legacy_fee_seed_accepts_missing_or_compatible_integer_mirror(legacy_fee):
    trade = row(fees_usd_fp="0.000125", fees_cents=legacy_fee)
    record_fill(trade, Fraction(1, 4))
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=None, fee=None))
    assert floors(trade) == (None, None, "0.000125", "0.25")
    assert confirmed_fill(trade) == Fraction(1, 2)
    cleared_current(trade)
    assert COST_QUARANTINE_NOTE not in (trade.notes or "")


@pytest.mark.parametrize("legacy_fee", [2, True, -1, "1", 0.0])
def test_invalid_legacy_fee_mirror_quarantines_without_seeding_or_rehabilitating(legacy_fee):
    trade = row(fees_usd_fp="0.000125", fees_cents=legacy_fee)
    record_fill(trade, Fraction(1, 4))
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=None, fee=None))
    assert confirmed_fill(trade) == Fraction(1, 2)
    assert requested_quantity(trade) == 1
    assert floors(trade) == (None, None, None, None)
    assert COST_QUARANTINE_NOTE in trade.notes
    cleared_current(trade)
    record_order_fill(trade, Fraction(3, 4), observation("0.75", gross="0.300000", fee="0.003000"))
    assert confirmed_fill(trade) == Fraction(3, 4)
    assert floors(trade) == (None, None, None, None)
    assert COST_QUARANTINE_NOTE in trade.notes
    cleared_current(trade)


def test_no_current_fee_does_not_compare_legacy_mirror_to_older_floor_quantity():
    trade = row()
    record_order_fill(trade, Fraction(1, 4), observation(fee="0.000125"))
    record_order_fill(trade, Fraction(1, 2), observation("0.50", gross=None, fee=None))
    assert floors(trade)[2:] == ("0.000125", "0.25")
    assert trade.fees_usd_fp is None
    trade.fees_cents = 2  # Sin FP actual, no se puede atribuir este espejo al fill .25.
    record_order_fill(trade, Fraction(3, 4), observation("0.75", gross="0.300000", fee="0.001500"))
    assert COST_QUARANTINE_NOTE not in (trade.notes or "")
    assert floors(trade)[2:] == ("0.001500", "0.75")
    assert trade.fees_usd_fp == "0.001500" and trade.fees_cents == 1
