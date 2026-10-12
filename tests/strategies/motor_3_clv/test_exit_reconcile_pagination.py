"""Un prefijo de órdenes nunca autoriza liberar o liquidar una venta incierta.

Clientes ficticios, SQLite temporal y evidencia de fills explícita. No hay red,
cancelaciones ni órdenes; el reconciliador conserva la atribución M2/M3 real.
"""

import asyncio
from fractions import Fraction
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
from src.storage.quantities import filled_fields, open_quantity, quantity_fields
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor
from tests.exit_fill_fixtures import wire_exit_fills

TICKER = "KX-EXIT-PAGINATION"
COID = "fake-exit-pending"


def seed(strategy="motor_3_clv"):
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id="fake-buy",
                ticker=TICKER,
                side="yes",
                action="buy",
                strategy="motor_2_consensus",
                price_cents=45,
                fill_price_cents=45,
                status="filled",
                **filled_fields(10),
            )
        )
        s.add(
            models.Trade(
                client_order_id=COID,
                ticker=TICKER,
                side="yes",
                action="sell",
                strategy=strategy,
                price_cents=60,
                status="pending",
                notes="clv_exit_intent",
                **quantity_fields(10),
            )
        )
        s.commit()


def rows():
    with models.get_session() as s:
        return list(s.exec(select(models.Trade).order_by(models.Trade.id)))


def order(fill="0.00", **overrides):
    return {
        "order_id": "fake-order",
        "client_order_id": COID,
        "ticker": TICKER,
        "side": "yes",
        "action": "sell",
        "status": "canceled",
        "initial_count_fp": "10.00",
        "fill_count_fp": fill,
        "remaining_count_fp": "0.00",
        "maker_fees_dollars": "0.00",
        "taker_fees_dollars": "0.00" if fill == "0.00" else "0.01",
        **overrides,
    }


def client(pages):
    c = MagicMock()
    c.get_orders = AsyncMock(side_effect=pages)
    c.place_order = AsyncMock(side_effect=AssertionError("ninguna orden nueva"))
    c.cancel_order = AsyncMock(side_effect=AssertionError("ninguna cancelación"))
    c.get_fills = AsyncMock(side_effect=AssertionError("sin fills antes de cerrar el barrido"))
    return c


@pytest.mark.parametrize("strategy", ["motor_2_consensus", "motor_3_clv"])
@pytest.mark.parametrize("fill", ["0.00", "0.25", "10.00"])
async def test_terminal_second_page_after_restart_keeps_exact_inventory(
    strategy, fill, _tmp_db_engine, monkeypatch
):
    seed(strategy)
    url = _tmp_db_engine.url
    _tmp_db_engine.dispose()
    reopened = create_engine(url)
    monkeypatch.setattr(models, "_engine", reopened)
    try:
        c = client([{"orders": [], "cursor": "next"}, {"orders": [order(fill)], "cursor": ""}])
        if fill != "0.00":
            wire_exit_fills(c, [("yes", Fraction(fill), 60, 1)])
        ex = Motor3ExitExecutor(c, strategy=strategy)
        assert await ex._reconcile_pending_sells(TICKER, "yes")
        before = [r.model_dump() for r in rows()]
        assert await ex._reconcile_pending_sells(TICKER, "yes")
        assert [r.model_dump() for r in rows()] == before
        assert [call.kwargs["cursor"] for call in c.get_orders.await_args_list] == [None, "next"]
        assert all(call.kwargs["ticker"] == TICKER for call in c.get_orders.await_args_list)
        assert all(call.kwargs["limit"] == 200 for call in c.get_orders.await_args_list)
        sell = next(r for r in rows() if r.action == "sell")
        assert sell.status == ("cancelled" if fill == "0.00" else "settled")
        remaining = sum(
            open_quantity(r) for r in rows() if r.action == "buy" and r.status == "filled"
        )
        assert remaining == 10 - Fraction(fill)
        c.place_order.assert_not_awaited()
        c.cancel_order.assert_not_awaited()
    finally:
        reopened.dispose()


@pytest.mark.parametrize(
    "second",
    [
        RuntimeError("página caída"),
        {"orders": [], "cursor": "next"},
        {"orders": []},
        {"orders": [], "cursor": None},
        {"orders": "malformada", "cursor": ""},
        {"orders": [None], "cursor": ""},
        {"orders": [order(client_order_id="otra-identidad")], "cursor": ""},
        {"orders": [order("0.25", order_id="otro-id")], "cursor": ""},
    ],
    ids=[
        "red",
        "ciclo",
        "cursor-ausente",
        "cursor-invalido",
        "lista",
        "fila",
        "oid-duplicado",
        "coid-contradictorio",
    ],
)
async def test_terminal_prefix_never_mutates_when_rest_of_sweep_fails(second):
    seed()
    before = [r.model_dump() for r in rows()]
    c = client([{"orders": [order()], "cursor": "next"}, second])
    out = await Motor3ExitExecutor(c).exit_position(
        models.PortfolioPosition(ticker=TICKER, side="yes", count=10)
    )
    assert out.reason == "pending_sell_unresolved" and not out.placed
    assert [r.model_dump() for r in rows()] == before
    c.place_order.assert_not_awaited()
    c.cancel_order.assert_not_awaited()
    c.get_fills.assert_not_awaited()


async def test_missing_final_cursor_does_not_release_zero_fill():
    seed()
    c = client([{"orders": [order()]}])
    assert not await Motor3ExitExecutor(c)._reconcile_pending_sells(TICKER, "yes")
    assert rows()[1].status == "pending"


async def test_complete_absence_keeps_pending_without_inventing_zero_fill():
    seed()
    c = client([{"orders": [], "cursor": "next"}, {"orders": [], "cursor": ""}])
    before = [r.model_dump() for r in rows()]
    assert not await Motor3ExitExecutor(c)._reconcile_pending_sells(TICKER, "yes")
    assert c.get_orders.await_count == 2
    assert [r.model_dump() for r in rows()] == before


async def test_page_budget_exhaustion_cannot_release_terminal_prefix():
    seed()
    pages = [{"orders": [order()] if i == 0 else [], "cursor": f"p{i + 1}"} for i in range(100)]
    c = client(pages)
    assert not await Motor3ExitExecutor(c)._reconcile_pending_sells(TICKER, "yes")
    assert c.get_orders.await_count == 100
    assert rows()[1].status == "pending"
    c.get_fills.assert_not_awaited()


async def test_total_timeout_keeps_terminal_prefix_pending(monkeypatch):
    import src.clients.kalshi_rest as rest

    seed()
    monkeypatch.setattr(rest, "_ORDERS_PAGINATION_TIMEOUT_SECONDS", 0.01)
    c = client([])

    async def delayed(**kwargs):
        if kwargs.get("cursor") is None:
            return {"orders": [order()], "cursor": "next"}
        await asyncio.Event().wait()

    c.get_orders.side_effect = delayed
    assert not await Motor3ExitExecutor(c)._reconcile_pending_sells(TICKER, "yes")
    assert rows()[1].status == "pending"
    c.get_fills.assert_not_awaited()


async def test_cancelled_sweep_propagates_without_mutating_terminal_prefix():
    seed()
    c = client([{"orders": [order()], "cursor": "next"}, asyncio.CancelledError()])
    before = [r.model_dump() for r in rows()]
    with pytest.raises(asyncio.CancelledError):
        await Motor3ExitExecutor(c)._reconcile_pending_sells(TICKER, "yes")
    assert [r.model_dump() for r in rows()] == before
    c.get_fills.assert_not_awaited()
