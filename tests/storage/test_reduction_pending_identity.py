"""Una atribución textual dañada no autoriza a liquidar dos veces la exposición."""

from fractions import Fraction
from unittest.mock import AsyncMock

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
from src.monitoring.health import BotState
from src.storage.order_reconciliation import capture_terminal_sell_execution
from src.storage.quantities import filled_fields, quantity_fields
from src.storage.reductions import persist_reduction


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'pending-reduction.db'}")
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="origin",
                ticker="OFFLINE-EXPOSURE",
                side="yes",
                action="buy",
                strategy="motor_rest_arb",
                price_cents=40,
                status="filled",
                **filled_fields(1),
            )
        )
        session.commit()
    yield
    engine.dispose()


def rows():
    with models.get_session() as session:
        return list(session.exec(select(models.Trade).order_by(models.Trade.id)))


async def test_quarantined_ack_keeps_single_pending_reduction_and_buy_intact():
    persist_reduction("origin", "first", 1, 1)
    client = AsyncMock()
    order = {
        "order_id": "known-id",
        "client_order_id": "first",
        "ticker": "OFFLINE-EXPOSURE",
        "side": "yes",
        "action": "sell",
        "subaccount_number": 0,
        "status": "canceled",
        "initial_count_fp": "1.00",
        "remaining_count_fp": "0.00",
        "fill_count_fp": "0.25",
        "maker_fees_dollars": "NaN",
        "taker_fees_dollars": "0.00",
    }
    with pytest.raises(ValueError, match="COSTS_QUARANTINED"):
        await capture_terminal_sell_execution(client, "first", order)
    before = [row.model_dump() for row in rows()]
    assert before[1]["status"] == "pending"
    assert before[1]["filled_count_fp"] == "0.25"
    assert before[1]["notes"].startswith("fill_cost_quarantined")
    assert models.kill_switch_engaged()[0] and BotState.is_paused
    with pytest.raises(ValueError, match="prohibido revender"):
        persist_reduction("origin", "second", Fraction(1), 1)
    assert [row.model_dump() for row in rows()] == before
    assert before[0]["status"] == "filled"
    client.get_fills.assert_not_awaited()
    client.get_historical_fills.assert_not_awaited()


@pytest.mark.parametrize(
    "notes", [None, "unrelated", "fill_cost_quarantined | reduction_for=origin"]
)
def test_pending_same_exposure_blocks_independently_of_notes(notes):
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="unknown-exit",
                ticker="OFFLINE-EXPOSURE",
                side="yes",
                action="sell",
                strategy="motor_rest_arb",
                price_cents=1,
                status="pending",
                notes=notes,
                **quantity_fields(1),
            )
        )
        session.commit()
    with pytest.raises(ValueError, match="prohibido revender"):
        persist_reduction("origin", "second", 1, 1)
    assert len(rows()) == 2


@pytest.mark.parametrize(
    "change",
    [{"ticker": "OTHER"}, {"side": "no"}, {"strategy": "motor_1_arbitrage"}],
)
def test_different_exposure_does_not_block_attributable_reduction(change):
    values = {
        "client_order_id": "other-exit",
        "ticker": "OFFLINE-EXPOSURE",
        "side": "yes",
        "action": "sell",
        "strategy": "motor_rest_arb",
        "price_cents": 1,
        "status": "pending",
        **quantity_fields(1),
        **change,
    }
    with models.get_session() as session:
        session.add(models.Trade(**values))
        session.commit()
    persist_reduction("origin", "new", 1, 1)
    assert len(rows()) == 3
