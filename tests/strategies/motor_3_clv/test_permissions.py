"""Gate opcional de salida: reserva y revisión durables, cliente ficticio."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text
from sqlmodel import select

from src.control.channel import Intent
from src.control.permissions import (
    Admission,
    DurablePermissionAdapter,
    GateFacts,
    PermissionBinding,
)
from src.storage.models import PortfolioPosition, Trade, get_session
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

NOW = 1791900000000


@pytest.fixture(params=[("motor_3_clv", "m3.exits"), ("motor_2_consensus", "m2.exits")])
def setup(_tmp_db_engine, request):
    strategy, scope = request.param
    binding = PermissionBinding(
        11, 22, "demo", "a" * 40, "b" * 32, "c" * 64, frozenset({scope}), frozenset({scope})
    )
    facts = [GateFacts(NOW, True, True, 0, 0, 0)]
    adapter = DurablePermissionAdapter(
        _tmp_db_engine, binding, lambda *_: facts[0], clock=lambda: NOW
    )
    adapter.provision()
    client = MagicMock()
    client.get_orderbook = AsyncMock(
        return_value={"orderbook": {"yes": [["0.60", "10"]], "no": []}}
    )
    client.place_order = AsyncMock(side_effect=TimeoutError("simulated lost ACK"))
    executor = Motor3ExitExecutor(client, strategy=strategy, permissions=adapter)
    with get_session() as s:
        s.add(
            Trade(
                client_order_id="original-buy",
                ticker="KXTEST",
                side="yes",
                action="buy",
                count=10,
                price_cents=45,
                fill_price_cents=45,
                strategy="motor_2_consensus",
                status="filled",
            )
        )
        s.commit()
    # Sustituto local explícito de observación de cuenta; no prueba capital/Demo.
    intent = Intent(
        "d" * 32, 11, 22, "demo", "a" * 40, "b" * 32, "c" * 64, scope, True, 0, NOW, NOW + 60000
    )
    return executor, adapter, intent, facts


def sells_and_audit():
    with get_session() as s:
        return s.exec(select(Trade).where(Trade.action == "sell")).all(), s.exec(
            select(Admission)
        ).all()


async def test_permission_disabled_denies_after_book_without_sell(setup):
    executor, _, _, _ = setup
    result = await executor.exit_position(PortfolioPosition(ticker="KXTEST", side="yes", count=10))
    assert result.reason == "permission:PERMISSION_DISABLED"
    executor.client.place_order.assert_not_called()
    assert sells_and_audit() == ([], [])


async def test_enabled_lost_ack_retains_trade_and_original_permission(setup):
    executor, adapter, intent, _ = setup
    adapter.apply_if_current(intent)
    result = await executor.exit_position(PortfolioPosition(ticker="KXTEST", side="yes", count=10))
    assert result.placed and not result.filled
    assert executor.client.place_order.await_count == 1
    sells, audit = sells_and_audit()
    assert len(sells) == len(audit) == 1
    assert sells[0].status == "pending"
    assert audit[0].client_order_id == sells[0].client_order_id
    assert (audit[0].scope, audit[0].permission_revision, audit[0].command_id) == (
        intent.scope,
        1,
        intent.command_id,
    )
    with get_session() as s:
        assert s.exec(select(Trade).where(Trade.action == "buy")).one().status == "filled"


@pytest.mark.parametrize("failure", ["facts_stale", "facts_unknown", "permission_deleted"])
async def test_change_during_book_wait_blocks_reservation(setup, failure):
    executor, adapter, intent, facts = setup
    adapter.apply_if_current(intent)

    async def book(*args, **kwargs):
        if failure == "permission_deleted":
            with adapter.engine.begin() as c:
                c.execute(text("DELETE FROM motor_permissions"))
        else:
            facts[0] = replace(
                facts[0],
                **(
                    {"observed_at_ms": NOW - 30001}
                    if failure == "facts_stale"
                    else {"gates_clear": False}
                ),
            )
        return {"orderbook": {"yes": [["0.60", "10"]], "no": []}}

    executor.client.get_orderbook.side_effect = book
    result = await executor.exit_position(PortfolioPosition(ticker="KXTEST", side="yes", count=10))
    assert not result.placed and result.reason.startswith("permission:")
    executor.client.place_order.assert_not_called()
    assert sells_and_audit() == ([], [])


async def test_permission_db_insert_failure_rolls_back_sell(setup):
    executor, adapter, intent, _ = setup
    adapter.apply_if_current(intent)
    with adapter.engine.begin() as c:
        c.execute(
            text(
                "CREATE TRIGGER fail_audit BEFORE INSERT ON motor_permission_admissions BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        )
    result = await executor.exit_position(PortfolioPosition(ticker="KXTEST", side="yes", count=10))
    assert result.reason == "persist_intent_failed"
    executor.client.place_order.assert_not_called()
    assert sells_and_audit() == ([], [])
