"""Repros contables de recuperación: SQLite temporal y fake GET, ninguna orden."""

from fractions import Fraction
from unittest.mock import Mock

import pytest
from sqlmodel import create_engine, select

from src.monitoring.health import BotState
from src.storage import models
from src.storage.order_costs import COST_QUARANTINE_NOTE
from src.storage.order_reconciliation import capture_terminal_sell_execution
from src.storage.quantities import confirmed_fill, filled_fields, quantity_fields
from src.storage.reductions import persist_reduction
from src.strategies.motor_3_clv import executor as m3
from src.utils.config import Settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'exit-recovery.db'}")
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    monkeypatch.setattr(
        "src.utils.config.get_settings", lambda: Settings.model_construct(TRADING_ENABLED=False)
    )
    denied = Mock(side_effect=AssertionError("OFFLINE_RECOVERY_NO_NETWORK"))
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


def persisted(coid):
    with models.get_session() as session:
        return session.exec(select(models.Trade).where(models.Trade.client_order_id == coid)).one()


def seed_sell():
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="m3-sell",
                kalshi_order_id="expected-order",
                ticker="FIXTURE-EXIT",
                side="yes",
                action="sell",
                strategy="motor_3_clv",
                price_cents=60,
                status="pending",
                **quantity_fields(1),
            )
        )
        session.commit()


def zero_order(**changes):
    value = {
        "client_order_id": "m3-sell",
        "order_id": "expected-order",
        "ticker": "FIXTURE-EXIT",
        "side": "yes",
        "action": "sell",
        "outcome_side": "no",
        "book_side": "ask",
        "subaccount_number": 0,
        "status": "canceled",
        "initial_count_fp": "1.00",
        "remaining_count_fp": "0.00",
        "fill_count_fp": "0.00",
    }
    value.update(changes)
    return value


class FakeGetOnly:
    def __init__(self, orders):
        self.orders = orders
        self.calls = []

    async def get_orders(self, **params):
        self.calls.append(params)
        return {"orders": self.orders}


@pytest.mark.parametrize(
    "changes",
    [
        {"ticker": "WRONG"},
        {"side": "no"},
        {"action": "buy"},
        {"order_id": "wrong-order"},
        {"subaccount_number": 1},
        {"subaccount_number": False},
        {"outcome_side": "yes"},
        {"book_side": "bid"},
    ],
)
async def test_m3_terminal_zero_identity_conflict_cannot_release_reservation(changes):
    seed_sell()
    before = persisted("m3-sell").model_dump()
    client = FakeGetOnly([zero_order(**changes)])
    assert (
        await m3.Motor3ExitExecutor(client)._reconcile_pending_sells("FIXTURE-EXIT", "yes") is False
    )
    assert persisted("m3-sell").model_dump() == before
    assert BotState.is_paused and models.kill_switch_engaged()[0]
    assert client.calls == [{"ticker": "FIXTURE-EXIT"}]


@pytest.mark.parametrize("conflicting", [False, True])
async def test_m3_duplicate_coid_has_no_terminal_verdict(conflicting):
    seed_sell()
    before = persisted("m3-sell").model_dump()
    second = zero_order(order_id="different-order") if conflicting else zero_order()
    client = FakeGetOnly([zero_order(), second])
    assert (
        await m3.Motor3ExitExecutor(client)._reconcile_pending_sells("FIXTURE-EXIT", "yes") is False
    )
    assert persisted("m3-sell").model_dump() == before
    assert BotState.is_paused and models.kill_switch_engaged()[0]


async def test_m3_valid_zero_still_cancels_once_without_fill_collection():
    seed_sell()
    client = FakeGetOnly([zero_order()])
    executor = m3.Motor3ExitExecutor(client)
    assert await executor._reconcile_pending_sells("FIXTURE-EXIT", "yes") is True
    row = persisted("m3-sell")
    assert row.status == "cancelled" and confirmed_fill(row) == 0
    assert not BotState.is_paused and not models.kill_switch_engaged()[0]
    before = row.model_dump()
    assert await executor._reconcile_pending_sells("FIXTURE-EXIT", "yes") is True
    assert persisted("m3-sell").model_dump() == before
    assert len(client.calls) == 1


async def test_quarantined_partial_reduction_blocks_second_intent_independent_of_notes():
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="original",
                ticker="FIXTURE-RB",
                side="yes",
                action="buy",
                strategy="motor_1_arbitrage",
                price_cents=40,
                status="filled",
                **filled_fields(1),
            )
        )
        session.commit()
    persist_reduction("original", "first-rb", 1, 1)
    order = zero_order(
        client_order_id="first-rb",
        order_id="first-order",
        ticker="FIXTURE-RB",
        fill_count_fp="0.25",
        maker_fees_dollars="NaN",
        taker_fees_dollars="0",
    )
    # Fake carece de get_fills: la cuarentena debe resolverse antes de cualquier GET.
    with pytest.raises(ValueError, match="COSTS_QUARANTINED"):
        await capture_terminal_sell_execution(FakeGetOnly([]), "first-rb", order, placement=True)
    first = persisted("first-rb")
    assert first.status == "pending" and confirmed_fill(first) == Fraction(1, 4)
    assert COST_QUARANTINE_NOTE in first.notes
    assert first.notes != "reduction_for=original"
    assert BotState.is_paused and models.kill_switch_engaged()[0]
    with pytest.raises(ValueError):
        persist_reduction("original", "second-rb", 1, 1)
    with models.get_session() as session:
        rows = list(session.exec(select(models.Trade).where(models.Trade.action == "sell")))
    assert [row.client_order_id for row in rows] == ["first-rb"]
    assert persisted("original").status == "filled"


def test_separate_ticker_is_not_blocked_by_unrelated_pending_sell():
    seed_sell()
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="other-buy",
                ticker="UNRELATED",
                side="yes",
                action="buy",
                strategy="motor_3_clv",
                price_cents=40,
                status="filled",
                **filled_fields(1),
            )
        )
        session.commit()
    persist_reduction("other-buy", "other-rb", 1, 1)
    assert persisted("other-rb").status == "pending"
