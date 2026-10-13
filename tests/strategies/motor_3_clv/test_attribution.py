"""Inventario compartido: no vender hedges ni convertir incertidumbre en cero."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine, select

from src.storage.models import Trade
from src.strategies.motor_3_clv.attribution import attributable_buys, attributable_quantity
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
POLICY = {"entry_origin": ("motor_2_consensus",), "include_motor1_orphans": True}


def buy(coid, **values):
    return Trade(
        **(
            {
                "client_order_id": coid,
                "ticker": "KX-E-A",
                "side": "yes",
                "action": "buy",
                "count": 1,
                "filled_count": 1,
                "price_cents": 30,
                "fees_cents": 0,
                "strategy": "motor_2_consensus",
                "status": "filled",
                "placed_at": NOW,
            }
            | values
        )
    )


@pytest.fixture
def ledger(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.exec(text("BEGIN IMMEDIATE"))
        yield session, engine
        session.rollback()
    engine.dispose()


def test_shared_fifo_quantities_orphans_and_hedges(ledger):
    session, _ = ledger
    rows = [
        buy("m2", count_fp="0.25", filled_count=None, filled_count_fp="0.25"),
        buy(
            "orphan",
            strategy="motor_1_arbitrage",
            notes="arb_id=o",
            placed_at=NOW - timedelta(minutes=1),
        ),
        buy("hedge-y", strategy="motor_1_arbitrage", notes="arb_id=h"),
        buy("hedge-n", strategy="motor_1_arbitrage", notes="arb_id=h", side="no"),
        buy("unidentified", strategy="motor_1_arbitrage", notes=None),
        buy("rest", strategy="motor_rest_arb"),
        buy("other-side", side="no"),
        buy("other-ticker", ticker="KX-E-B"),
        buy("closed", closed_by_clv=True),
        buy("pending", status="pending", filled_count=None),
    ]
    session.add_all(rows)
    session.flush()
    selected = attributable_buys(session, "KX-E-A", "yes", **POLICY)
    assert [b.client_order_id for b in selected] == ["orphan", "m2"]
    assert attributable_quantity(session, "KX-E-A", "yes", **POLICY) == Fraction(5, 4)
    for strategy in ("motor_2_consensus", "motor_3_clv"):
        executor = Motor3ExitExecutor(MagicMock(), strategy=strategy, **POLICY)
        assert executor._attributable_buys(session, "KX-E-A", "yes") == selected
    assert attributable_quantity(
        session, "KX-E-A", "yes", **(POLICY | {"include_motor1_orphans": False})
    ) == Fraction(1, 4)
    assert (
        attributable_buys(
            session,
            "KX-E-A",
            "yes",
            **(POLICY | {"entry_origin": (), "include_motor1_orphans": False}),
        )
        == []
    )


@pytest.mark.parametrize(
    "values",
    [
        {"count_fp": "1.00", "filled_count": None},
        {"count": 0, "count_fp": "0.25", "filled_count": None, "filled_count_fp": "0.25"},
        {"count": -1},
        {"count_fp": "bad"},
        {"filled_count": 2},
        {"filled_count": 1, "filled_count_fp": "0.25"},
    ],
)
def test_unknown_or_contradictory_fill_is_never_zero(ledger, values):
    session, _ = ledger
    session.add(buy("bad", **values))
    session.flush()
    with pytest.raises(ValueError):
        attributable_quantity(session, "KX-E-A", "yes", **POLICY)
    with pytest.raises(ValueError):
        Motor3ExitExecutor(MagicMock(), **POLICY)._attributable_buys(session, "KX-E-A", "yes")


@pytest.mark.parametrize("mutation", ["new", "dirty", "deleted"])
def test_gate_reader_never_flushes_financial_changes(ledger, mutation):
    session, engine = ledger
    row = buy("row")
    if mutation == "new":
        session.add(row)
    else:
        session.add(row)
        session.flush()
        if mutation == "dirty":
            row.count = 2
        else:
            session.delete(row)
    sql = []
    event.listen(engine, "before_cursor_execute", lambda c, cu, st, p, ctx, many: sql.append(st))
    with pytest.raises(ValueError, match="UNFLUSHED_FINANCIAL_STATE"):
        attributable_quantity(session, "KX-E-A", "yes", **POLICY)
    assert not sql and row in getattr(session, mutation)


def test_borrowed_reader_sees_flush_and_leaves_commit_to_caller(ledger):
    session, engine = ledger
    session.add(buy("row"))
    session.flush()
    sql = []
    event.listen(engine, "before_cursor_execute", lambda c, cu, st, p, ctx, many: sql.append(st))
    assert attributable_quantity(session, "KX-E-A", "yes", **POLICY) == 1
    assert all(st.lstrip().upper().startswith("SELECT") for st in sql)
    assert session.connection().connection.driver_connection.in_transaction
    session.rollback()
    assert session.exec(select(Trade)).all() == []


def test_zero_confirmed_fill_is_not_inventory(ledger):
    session, _ = ledger
    session.add(buy("zero", filled_count=0))
    session.flush()
    assert attributable_quantity(session, "KX-E-A", "yes", **POLICY) == 0


def test_unknown_opposite_m1_leg_cannot_turn_hedge_into_orphan(ledger):
    session, _ = ledger
    session.add_all(
        [
            buy("hedge-y", strategy="motor_1_arbitrage", notes="arb_id=h"),
            buy(
                "hedge-n",
                strategy="motor_1_arbitrage",
                notes="arb_id=h",
                side="no",
                count_fp="1.00",
                filled_count=None,
            ),
        ]
    )
    session.flush()
    with pytest.raises(ValueError):
        attributable_quantity(session, "KX-E-A", "yes", **POLICY)
    assert (
        attributable_quantity(
            session, "KX-E-A", "yes", **(POLICY | {"include_motor1_orphans": False})
        )
        == 0
    )
