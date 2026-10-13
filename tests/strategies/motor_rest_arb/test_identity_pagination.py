"""Una respuesta contradictoria/incompleta no libera ni liquida una compra REST.

Sólo clientes ficticios y SQLite temporal; cero órdenes o consultas externas.
"""

import asyncio
from fractions import Fraction
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import select

import src.storage.models as models
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.monitoring.health import BotState
from src.storage.quantities import confirmed_fill, record_fill
from src.strategies.motor_rest_arb.executor import (
    ExecutionOutcome,
    LegResult,
    LegState,
    RestExecutor,
)

COID = "fake-rest-buy"
OID = "fake-rest-order"
TICKER = "KX-REST-IDENTITY"


@pytest.fixture(autouse=True)
def pause_state(monkeypatch):
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)


def client(pages):
    c = MagicMock()
    c.get_orders = AsyncMock(side_effect=pages)
    c.place_order = AsyncMock(side_effect=AssertionError("sin nueva orden"))
    c.cancel_order = AsyncMock(side_effect=AssertionError("sin cancelación"))
    return c


def seeded(c, *, side="yes", known_oid=True):
    ex = RestExecutor(c)
    leg = ArbLeg(TICKER, side, 40, 5, 10)
    opp = ArbOpportunity((leg,), 5, 10, 1, 9, 2.0)
    assert ex._persist_intents("fake-arb", opp, {leg: COID})
    if known_oid:
        with models.get_session() as s:
            row = s.exec(select(models.Trade).where(models.Trade.client_order_id == COID)).one()
            row.kalshi_order_id = OID
            s.add(row)
            s.commit()
    return ex, LegResult(leg, LegState.ERROR_RED, COID, OID if known_oid else None, None, False)


def persisted():
    with models.get_session() as s:
        return s.exec(select(models.Trade).where(models.Trade.client_order_id == COID)).one()


def order(fill="0.00", *, side="yes", **changes):
    return {
        "client_order_id": COID,
        "order_id": OID,
        "ticker": TICKER,
        "side": side,
        "action": "buy",
        "outcome_side": side,
        "book_side": "bid" if side == "yes" else "ask",
        "subaccount_number": 0,
        "initial_count_fp": "5.00",
        "fill_count_fp": fill,
        "remaining_count_fp": "0.00",
        "status": "canceled",
        **changes,
    }


CONFLICTS = [
    {"client_order_id": "other-coid"},
    {"ticker": "OTHER"},
    {"side": "no"},
    {"action": "sell"},
    {"order_id": "other-oid"},
    {"order_id": ""},
    {"outcome_side": "no"},
    {"book_side": "ask"},
    {"subaccount_number": 1},
    {"subaccount_number": False},
]


@pytest.mark.parametrize("changes", CONFLICTS)
@pytest.mark.parametrize("fill", ["0.00", "0.25"])
async def test_reconcile_conflict_keeps_reservation_and_never_rolls_back(changes, fill):
    c = client([{"orders": [order(fill, **changes)], "cursor": ""}])
    ex, result = seeded(c)
    before = persisted().model_dump()
    outcome = ExecutionOutcome(False)
    assert await ex._resolve_exposure([result], outcome) == []
    assert outcome.kill_switch_fired
    assert models.kill_switch_engaged()[0]
    assert persisted().model_dump() == before
    c.place_order.assert_not_awaited()
    c.cancel_order.assert_not_awaited()


@pytest.mark.parametrize("changes", CONFLICTS)
@pytest.mark.parametrize("fill", ["0.00", "5.00"])
async def test_placement_conflict_cannot_be_fill_or_kill(changes, fill):
    c = client([])
    ex, intent = seeded(c)
    before = persisted().model_dump()
    c.place_order = AsyncMock(return_value={"order": order(fill, **changes)})
    result = await ex._place_fok(intent.leg, COID)
    ex._apply_leg_result_to_trade(result)
    assert result.state is LegState.ERROR_RED and not result.terminal
    assert result.fill_count is None and result.order_id is None
    assert persisted().model_dump() == before
    c.place_order.assert_awaited_once()


@pytest.mark.parametrize("side", ["yes", "no"])
@pytest.mark.parametrize("fill", ["0.00", "0.25", "5.00"])
async def test_valid_terminal_second_page_preserves_exact_quantity(side, fill):
    c = client(
        [{"orders": [], "cursor": "next"}, {"orders": [order(fill, side=side)], "cursor": ""}]
    )
    ex, result = seeded(c, side=side)
    outcome = ExecutionOutcome(False)
    exposed = await ex._resolve_exposure([result], outcome)
    assert not outcome.kill_switch_fired
    if fill == "0.00":
        assert exposed == [] and persisted().status == "cancelled"
    else:
        assert len(exposed) == 1 and exposed[0].leg.count == Fraction(fill)
        assert persisted().status == "filled"
        assert confirmed_fill(persisted()) == Fraction(fill)
    assert [call.kwargs["cursor"] for call in c.get_orders.await_args_list] == [None, "next"]
    c.place_order.assert_not_awaited()


@pytest.mark.parametrize(
    "second",
    [
        RuntimeError("page unavailable"),
        {"orders": [], "cursor": "next"},
        {"orders": []},
        {"orders": [], "cursor": None},
        {"orders": "invalid", "cursor": ""},
        {"orders": [None], "cursor": ""},
        {"orders": [order()], "cursor": ""},
        {"orders": [order(client_order_id="other-coid")], "cursor": ""},
    ],
)
async def test_terminal_prefix_cannot_release_before_complete_sweep(second):
    c = client([{"orders": [order()], "cursor": "next"}, second])
    ex, result = seeded(c)
    before = persisted().model_dump()
    outcome = ExecutionOutcome(False)
    assert await ex._resolve_exposure([result], outcome) == []
    assert outcome.kill_switch_fired
    assert persisted().model_dump() == before
    c.place_order.assert_not_awaited()


async def test_complete_absence_is_unknown_not_zero_fill():
    c = client([{"orders": [], "cursor": "next"}, {"orders": [], "cursor": ""}])
    ex, result = seeded(c)
    before = persisted().model_dump()
    outcome = ExecutionOutcome(False)
    assert await ex._resolve_exposure([result], outcome) == []
    assert outcome.kill_switch_fired and c.get_orders.await_count == 2
    assert persisted().model_dump() == before


async def test_nonterminal_placement_keeps_correlated_oid_and_fill_floor():
    c = client([])
    ex, intent = seeded(c, known_oid=False)
    c.place_order = AsyncMock(
        return_value={"order": order("0.25", status="resting", remaining_count_fp="4.75")}
    )
    result = await ex._place_fok(intent.leg, COID)
    ex._apply_leg_result_to_trade(result)
    assert not result.terminal
    assert persisted().status == "pending"
    assert persisted().kalshi_order_id == OID
    assert confirmed_fill(persisted()) == Fraction(1, 4)


async def test_reconcile_never_erases_confirmed_fill_floor():
    c = client([{"orders": [order()], "cursor": ""}])
    ex, result = seeded(c)
    with models.get_session() as s:
        row = s.exec(select(models.Trade).where(models.Trade.client_order_id == COID)).one()
        record_fill(row, Fraction(1, 4))
        s.add(row)
        s.commit()
    before = persisted().model_dump()
    assert await ex._reconcile_leg(result) is None
    assert persisted().model_dump() == before


async def test_cancelled_pagination_preserves_intent_and_propagates():
    c = client([{"orders": [order()], "cursor": "next"}, asyncio.CancelledError()])
    ex, result = seeded(c)
    before = persisted().model_dump()
    with pytest.raises(asyncio.CancelledError):
        await ex._reconcile_leg(result)
    assert persisted().model_dump() == before


async def test_unknown_quantity_still_preserves_valid_ack_identity():
    c = client([])
    ex, intent = seeded(c, known_oid=False)
    c.place_order = AsyncMock(return_value={"order": {"order_id": OID, "status": "resting"}})
    result = await ex._place_fok(intent.leg, COID)
    ex._apply_leg_result_to_trade(result)
    assert persisted().kalshi_order_id == OID
    assert persisted().status == "pending" and confirmed_fill(persisted()) is None


@pytest.mark.parametrize("fill", ["0.00", "0.25"])
async def test_reconcile_preserves_new_oid_before_finalizing(fill):
    c = client([{"orders": [order(fill)], "cursor": ""}])
    ex, result = seeded(c, known_oid=False)
    outcome = ExecutionOutcome(False)
    await ex._resolve_exposure([result], outcome)
    assert not outcome.kill_switch_fired
    assert persisted().kalshi_order_id == OID


@pytest.mark.parametrize("fill", ["0.00", "5.00"])
async def test_execute_bad_hard_ack_never_submits_cheap_legs_or_sells(fill):
    c = client([{"orders": [], "cursor": ""}])
    c.place_order = AsyncMock(return_value={"order": order(fill, ticker="OTHER")})
    hard = ArbLeg(TICKER, "yes", 60, 5, 10)
    cheap = ArbLeg("KX-REST-OTHER", "yes", 30, 5, 10)
    ex = RestExecutor(c)
    result = await ex.execute(ArbOpportunity((hard, cheap), 5, 10, 1, 9, 2.0))
    assert not result.filled and result.kill_switch_fired and ex.is_paused
    assert c.place_order.await_count == 1
    with models.get_session() as s:
        rows = {r.ticker: r for r in s.exec(select(models.Trade)).all()}
    assert rows[TICKER].status == "pending" and rows[TICKER].kalshi_order_id is None
    assert rows[cheap.market_ticker].status == "cancelled"


@pytest.mark.parametrize("fail_ticker", [TICKER, "KX-REST-OTHER"])
async def test_post_fill_commit_failure_stops_cheap_legs(monkeypatch, fail_ticker):
    from sqlmodel import Session

    c = client([])
    c.place_order = AsyncMock(
        return_value={
            "order": {"order_id": OID, "fill_count_fp": "5.00", "remaining_count_fp": "0.00"}
        }
    )
    commit = Session.commit

    def failing_fill_commit(session):
        if any(
            isinstance(r, models.Trade) and r.status == "filled" and r.ticker == fail_ticker
            for r in session.dirty
        ):
            raise RuntimeError("fake post-fill commit failure")
        return commit(session)

    monkeypatch.setattr(Session, "commit", failing_fill_commit)
    hard = ArbLeg(TICKER, "yes", 60, 5, 10)
    cheap = ArbLeg("KX-REST-OTHER", "yes", 30, 5, 10)
    ex = RestExecutor(c)
    result = await ex.execute(ArbOpportunity((hard, cheap), 5, 10, 1, 9, 2.0))
    assert not result.filled and result.kill_switch_fired and ex.is_paused
    assert models.kill_switch_engaged()[0] and BotState.is_paused
    assert c.place_order.await_count == (1 if fail_ticker == TICKER else 2)
    with models.get_session() as s:
        rows = {r.ticker: r for r in s.exec(select(models.Trade)).all()}
    assert rows[fail_ticker].status == "pending"
    assert rows[TICKER].status == ("pending" if fail_ticker == TICKER else "filled")
    assert rows[cheap.market_ticker].status == ("cancelled" if fail_ticker == TICKER else "pending")


@pytest.mark.parametrize("response", [None, [], {"order": None}, {"order": []}])
async def test_malformed_ack_cannot_release_reservation(response):
    c = client([])
    ex, intent = seeded(c)
    before = persisted().model_dump()
    c.place_order = AsyncMock(return_value=response)
    result = await ex._place_fok(intent.leg, COID)
    ex._apply_leg_result_to_trade(result)
    assert result.state is LegState.ERROR_RED and not result.terminal
    assert persisted().model_dump() == before


async def test_duplicate_coid_with_distinct_oids_is_unknown():
    c = client(
        [
            {"orders": [order()], "cursor": "next"},
            {"orders": [order(order_id="other-oid")], "cursor": ""},
        ]
    )
    ex, result = seeded(c)
    before = persisted().model_dump()
    assert await ex._reconcile_leg(result) is None
    assert persisted().model_dump() == before


async def test_exhausted_page_budget_never_resolves_terminal_prefix():
    c = client([{"orders": [order()] if i == 0 else [], "cursor": f"next-{i}"} for i in range(100)])
    ex, result = seeded(c)
    before = persisted().model_dump()
    assert await ex._reconcile_leg(result) is None
    assert c.get_orders.await_count == 100
    assert persisted().model_dump() == before


async def test_sweep_timeout_cancels_pending_read_without_resolving_prefix(monkeypatch):
    monkeypatch.setattr("src.clients.kalshi_rest._ORDERS_PAGINATION_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()
    c = client([])

    async def pages(**kwargs):
        if kwargs["cursor"] is None:
            return {"orders": [order()], "cursor": "next"}
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    c.get_orders = AsyncMock(side_effect=pages)
    ex, result = seeded(c)
    before = persisted().model_dump()
    assert await ex._reconcile_leg(result) is None
    assert cancelled.is_set() and c.get_orders.await_count == 2
    assert persisted().model_dump() == before
