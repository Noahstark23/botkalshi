"""Cierres contables offline: SQLite en memoria, sin clientes ni órdenes.

Ejercita finalizadores M3/M2 y reductions reales. Una fee FP ligada al fill es
observada dentro de la fixture; el precio SELL sigue siendo límite estimado.
No acredita precios de ejecución, semántica de gross SELL ni contabilidad live.
"""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from unittest.mock import Mock

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
from src.math.quantities import fixed_count
from src.storage.quantities import confirmed_fill, filled_fields, open_quantity, quantity_fields
from src.storage.reductions import finalize_reduction, persist_reduction
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

NOW = datetime(2026, 10, 8, 16, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    engine = create_engine("sqlite://")
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    denied = Mock(side_effect=AssertionError("OFFLINE_ACCOUNTING_ONLY"))
    for target in (
        "socket.socket.connect",
        "socket.socket.connect_ex",
        "socket.create_connection",
        "socket.getaddrinfo",
        "src.clients.kalshi_rest.KalshiRestClient.__init__",
    ):
        monkeypatch.setattr(target, denied)
    yield
    engine.dispose()
    denied.assert_not_called()


def seed_buy(coid="buy", *, count=1, gross="0.400000", fee="0.002000", side="yes", age=0):
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id=coid,
                ticker="FIXTURE",
                side=side,
                action="buy",
                strategy="motor_2_consensus",
                price_cents=40,
                status="filled",
                fill_cost_usd_fp=gross,
                fees_usd_fp=fee,
                fees_cents=1,
                commitment_origin_id=f"origin-{coid}",
                placed_at=NOW + timedelta(seconds=age),
                **filled_fields(count),
            )
        )
        session.commit()


def prepare(mode, sold, *, observed=True, side="yes"):
    seed_buy(side=side)
    if mode == "reduction":
        persist_reduction("buy", "sell", 1, 60)
    else:
        with models.get_session() as session:
            session.add(
                models.Trade(
                    client_order_id="sell",
                    ticker="FIXTURE",
                    side=side,
                    action="sell",
                    strategy="motor_3_clv",
                    price_cents=60,
                    status="pending",
                    notes="clv_exit_intent",
                    **quantity_fields(1),
                )
            )
            session.commit()
    if observed:
        update_sell(filled_count_fp=fixed_count(sold), fees_usd_fp="0.001234", fees_cents=1)


def update_sell(**fields):
    with models.get_session() as session:
        row = session.exec(select(models.Trade).where(models.Trade.client_order_id == "sell")).one()
        for field, value in fields.items():
            setattr(row, field, value)
        session.add(row)
        session.commit()


def finish(mode, sold, *, side="yes", order_id="fixture-order"):
    if mode == "reduction":
        finalize_reduction("sell", sold, order_id)
    else:
        Motor3ExitExecutor(object())._finalize_fill("sell", "FIXTURE", side, sold, 60, order_id)


def rows():
    with models.get_session() as session:
        return list(session.exec(select(models.Trade).order_by(models.Trade.id)))


def snapshot():
    return [row.model_dump() for row in rows()]


@pytest.mark.parametrize("mode", ["m3", "reduction"])
@pytest.mark.parametrize("sold", [Fraction(1, 4), Fraction(1)])
@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("side", ["yes", "no"])
def test_partial_full_exit_retains_explicit_price_estimate(mode, sold, observed, side):
    prepare(mode, sold, observed=observed, side=side)
    if mode == "reduction":
        assert next(row for row in rows() if row.action == "sell").notes == "reduction_for=buy"
    finish(mode, sold, side=side)
    closed = next(row for row in rows() if row.action == "buy" and row.status == "settled")
    sell = next(row for row in rows() if row.action == "sell")
    # Oráculo independiente: precio supuesto 60c, costo/fee de entrada por fracción.
    exit_fee = Fraction("0.001234") if observed else Fraction("0.01" if sold < 1 else "0.02")
    expected = sold * Fraction("0.60") - sold * Fraction("0.402000") - exit_fee
    assert Fraction(closed.pnl_usd_fp) == expected
    assert closed.commitment_origin_id == "origin-buy"
    assert confirmed_fill(closed) == confirmed_fill(sell) == sold
    source = "observed_fp" if observed else "formula"
    for row in (closed, sell):
        assert "exit_accounting_estimated" in row.notes
        assert "exit_price=limit" in row.notes and f"exit_fee={source}" in row.notes
    if observed:
        assert sell.fees_usd_fp == "0.001234" and sell.fees_cents == 1
    else:
        assert sell.fees_usd_fp is None
    if sold < 1:
        remaining = next(row for row in rows() if row.status == "filled")
        assert open_quantity(remaining) == 1 - sold
        assert remaining.fill_cost_usd_fp == "0.300000"
        assert remaining.fees_usd_fp == "0.001500"
        assert "exit_accounting_estimated" not in (remaining.notes or "")
    before = snapshot()
    finish(mode, sold, side=side)
    assert snapshot() == before  # Replay no vuelve a repartir fees ni cerrar otro BUY.
    with pytest.raises(ValueError):
        finish(mode, sold, side=side, order_id="different-order")
    assert snapshot() == before


@pytest.mark.parametrize("mode", ["m3", "reduction"])
@pytest.mark.parametrize(
    "changes",
    [
        {"fees_usd_fp": "NaN"},
        {"fees_usd_fp": "-0.000001"},
        {"fees_usd_fp": "0.0000001"},
        {"fees_usd_fp": "0.50"},  # Mayor que cantidad .25 ejecutada.
        {"fees_cents": 2},  # .001234 USD permite espejo 0 o 1, nunca 2.
        {"filled_count_fp": None, "filled_count": None},
        {"filled_count_fp": "0.10"},
    ],
)
def test_invalid_or_unassociated_fee_rolls_back_entire_close(mode, changes):
    prepare(mode, Fraction(1, 4))
    update_sell(**changes)
    before = snapshot()
    with pytest.raises(ValueError):
        finish(mode, Fraction(1, 4))
    assert snapshot() == before
    if mode == "reduction":
        assert next(row for row in rows() if row.action == "sell").notes == "reduction_for=buy"


def test_fifo_exact_fee_residue_and_ambiguous_sell_gross_are_not_income():
    seed_buy("first", count=Fraction(1, 4), gross="0.100000", fee="0.000500")
    seed_buy("second", age=1)
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="sell",
                ticker="FIXTURE",
                side="yes",
                action="sell",
                strategy="motor_3_clv",
                price_cents=60,
                status="pending",
                filled_count_fp="0.75",
                fees_usd_fp="0.001001",
                fees_cents=1,
                fill_cost_usd_fp="0.660000",
                **quantity_fields(1),
            )
        )
        session.commit()
    finish("m3", Fraction(3, 4))
    closed = [row for row in rows() if row.action == "buy" and row.status == "settled"]
    # FIFO .25 + .50; fee .001001 se reparte .000333 + .000668, sin perder residuo.
    first = next(row for row in closed if row.commitment_origin_id == "origin-first")
    second = next(row for row in closed if row.commitment_origin_id == "origin-second")
    assert first.pnl_usd_fp == "0.049167"
    assert second.pnl_usd_fp == "0.098332"
    assert sum(Fraction(row.pnl_usd_fp) for row in closed) == Fraction("0.147499")
    # .660000 conserva su valor ambiguo: NO sustituye al ingreso estimado .75 * .60.
    sell = next(row for row in rows() if row.action == "sell")
    assert sell.fill_cost_usd_fp == "0.660000"
    assert sell.fees_usd_fp == "0.001001" and sell.fees_cents == 1
    remaining = next(row for row in rows() if row.status == "filled")
    assert open_quantity(remaining) == Fraction(1, 2)
    assert remaining.fill_cost_usd_fp == "0.200000"
    assert remaining.fees_usd_fp == "0.001000"


def test_provenance_survives_long_notes():
    prepare("m3", Fraction(1))
    with models.get_session() as session:
        for row in session.exec(select(models.Trade)):
            row.notes = "x" * 500
            session.add(row)
        session.commit()
    finish("m3", Fraction(1))
    assert all(row.notes.startswith("exit_accounting_estimated") for row in rows())
    assert all(len(row.notes) <= 500 for row in rows())


@pytest.mark.parametrize("changed", ["ticker", "side", "action", "price"])
def test_m3_identity_or_price_mismatch_never_writes_buys(changed):
    prepare("m3", Fraction(1, 4))
    if changed == "action":
        update_sell(action="buy")
    before = snapshot()
    with pytest.raises(ValueError, match="contradice intent persistido"):
        Motor3ExitExecutor(object())._finalize_fill(
            "sell",
            "OTHER" if changed == "ticker" else "FIXTURE",
            "no" if changed == "side" else "yes",
            Fraction(1, 4),
            61 if changed == "price" else 60,
            "fixture-order",
        )
    assert snapshot() == before
