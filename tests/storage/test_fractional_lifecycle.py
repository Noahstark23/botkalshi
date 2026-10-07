"""Centésimas de contrato de HTTP a salida/reinicio; sólo fakes y SQLite temporal."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from math import ceil
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
from src.clients.kalshi_rest import build_order_body
from src.math.quantities import api_count, terminal_fill
from src.monitoring.health import BotState
from src.risk.exposure import open_risk_cents
from src.storage.order_reconciliation import reconcile_pending_orders
from src.storage.quantities import (
    filled_fields,
    filled_quantity,
    open_quantity,
    quantity_fields,
    requested_quantity,
)
from src.storage.reductions import finalize_reduction, persist_reduction
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor
from src.strategies.motor_2_consensus.executor import Motor2Executor
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor
from src.strategies.motor_3_clv.poller import PortfolioPoller
from src.strategies.motor_rest_arb.executor import RestExecutor
from src.strategies.motor_rest_arb.settlement import _leg_pnl_cents
from tests.strategies.motor_2_consensus.test_executor_sizing_fill import (
    _FakeClient,
    _FakeRisk,
    _signal,
)
from tests.strategies.motor_rest_arb.test_execute_integration import _opp


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path}/ledger.db")
    models.SQLModel.metadata.create_all(db)
    monkeypatch.setattr(models, "_engine", db)
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr("src.monitoring.telegram_alerts.send_alert", AsyncMock())
    monkeypatch.setattr(
        "socket.socket.connect", Mock(side_effect=AssertionError("network forbidden"))
    )
    yield db
    db.dispose()


def buy(quantity="0.25", *, coid="buy", strategy="motor_2_consensus"):
    row = models.Trade(
        client_order_id=coid,
        ticker="T",
        side="yes",
        action="buy",
        price_cents=45,
        fill_price_cents=45,
        fees_cents=1,
        strategy=strategy,
        status="filled",
        **filled_fields(Fraction(quantity)),
    )
    with models.get_session() as s:
        s.add(row)
        s.commit()
    return row


def rows():
    with models.get_session() as s:
        return list(s.exec(select(models.Trade)))


@pytest.mark.parametrize("quantity", ["0.01", "0.25", "0.50", "0.75", "1.25"])
async def test_m2_fill_position_risk_and_settlement_exact(quantity):
    client = _FakeClient(
        {"order": {"remaining_count": 0, "fill_count": None, "fill_count_fp": quantity}}
    )
    out = await Motor2Executor(client, _FakeRisk()).execute(_signal(size_usd=3.0, price=45))
    q = Fraction(quantity)
    assert out.filled and out.filled_count == q
    (row,) = rows()
    assert row.status == "filled" and row.filled_count_fp == quantity
    assert open_quantity(row) == q and row.count == ceil(q)
    assert open_risk_cents(row) == ceil(q * 45)
    assert _leg_pnl_cents(row, "yes") == q * 55 - row.fees_cents


@pytest.mark.parametrize(
    "order",
    [
        {},
        {"remaining_count": 0, "fill_count_fp": "garbage"},
        {"remaining_count": 0, "fill_count_fp": "0.001"},
        {"remaining_count": 0, "fill_count_fp": "0.25", "fill_count": 0},
        {"remaining_count": 0, "fill_count_fp": "7.00"},
    ],
)
async def test_m2_invalid_fill_never_becomes_zero(order):
    out = await Motor2Executor(_FakeClient({"order": order}), _FakeRisk()).execute(
        _signal(size_usd=3.0, price=45)
    )
    (row,) = rows()
    assert out.uncertain and row.status == "pending"
    assert row.filled_count_fp is None


@pytest.mark.parametrize("fill", ["0.00", "0.25"])
async def test_m2_nonterminal_response_retains_fill_floor_and_full_reservation(fill):
    order = {"fill_count_fp": fill, "remaining_count_fp": "0.75", "status": "resting"}
    result = await Motor2Executor(_FakeClient({"order": order}), _FakeRisk()).execute(
        _signal(size_usd=3.0, price=45)
    )
    row = rows()[0]
    assert result.uncertain and row.status == "pending"
    assert requested_quantity(row) == 6 and row.filled_count_fp == fill
    assert open_risk_cents(row) == 270 and models.kill_switch_engaged()[0]


@pytest.mark.parametrize("fill", ["0.00", "0.25"])
async def test_m3_nonterminal_sell_never_closes_buy_or_sends_second_sell(fill):
    buy("1.00")
    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"yes": [[60, 10]], "no": []}}
    client.place_order.return_value = {
        "order": {
            "status": "resting",
            "fill_count_fp": fill,
            "remaining_count_fp": "0.75",
        }
    }
    ex = Motor3ExitExecutor(client)
    position = models.PortfolioPosition(ticker="T", side="yes", **quantity_fields(1))
    result = await ex.exit_position(position)
    assert not result.filled
    await ex.exit_position(position)
    assert client.place_order.await_count == 1
    original = next(r for r in rows() if r.action == "buy")
    pending = next(r for r in rows() if r.action == "sell")
    assert original.status == "filled" and open_quantity(original) == 1
    assert pending.status == "pending" and requested_quantity(pending) == 1
    assert pending.filled_count_fp == fill and models.kill_switch_engaged()[0]


@pytest.mark.parametrize("motor", ["m1", "rest"])
@pytest.mark.parametrize("fill", ["0.00", "0.25"])
async def test_arbitrage_nonterminal_hard_leg_never_triggers_guessed_rollback(motor, fill):
    order = {"status": "resting", "fill_count_fp": fill, "remaining_count_fp": "0.75"}
    client = AsyncMock()
    client.place_order.return_value = {"order": order}
    client.get_orders.return_value = {"orders": []}
    opp = _opp()
    if motor == "rest":
        result = await RestExecutor(client).execute(opp)
        assert not result.filled and not result.rollback_triggered
        assert client.place_order.await_count == 1
        row = next(r for r in rows() if r.status == "pending")
    else:
        from src.math.arbitrage import select_hard_leg

        leg, _ = select_hard_leg(opp)
        with models.get_session() as s:
            s.add(
                models.Trade(
                    client_order_id="hard",
                    ticker=leg.market_ticker,
                    side=leg.side,
                    action="buy",
                    price_cents=leg.price_cents,
                    strategy="motor_1_arbitrage",
                    status="pending",
                    **quantity_fields(leg.count),
                )
            )
            s.commit()
        state, _ = ArbitrageExecutor(client, Mock())._process_leg_result(
            leg, "hard", leg.count, {"order": order}
        )
        assert state == "error" and not client.place_order.called
        row = rows()[0]
    assert row.status == "pending" and row.filled_count_fp == fill
    assert requested_quantity(row) == opp.count
    assert models.kill_switch_engaged()[0]


def test_terminal_evidence_requires_final_counts_and_does_not_ignore_status():
    assert terminal_fill({"fill_count_fp": ".25"}, 1, placement=True) is None
    assert terminal_fill(
        {"fill_count_fp": ".25", "remaining_count_fp": "0.00"}, 1, placement=True
    ) == Fraction(1, 4)
    assert terminal_fill({"fill_count_fp": ".25", "remaining_count_fp": "0.00"}, 1) is None
    for order in (
        {"status": "resting", "fill_count_fp": ".25", "remaining_count_fp": "0.00"},
        {"status": "canceled", "fill_count_fp": ".25", "remaining_count_fp": ".75"},
        {"status": "canceled", "fill_count_fp": ".25", "initial_count_fp": "2.00"},
        {"status": "executed", "fill_count_fp": ".25", "initial_count_fp": "1.00"},
        {"status": "canceled", "fill_count_fp": ".25", "remaining_count_fp": None},
    ):
        assert terminal_fill(order, 1, placement=True) is None


def test_fp_null_legacy_and_v2_fractional_exit_payload():
    from decimal import Decimal

    from src.clients.kalshi_rest import KalshiOrderConfirmationError, _order_count

    assert api_count(
        {"remaining_count": 0, "fill_count": None, "fill_count_fp": "0.25"}, "fill_count"
    ) == Fraction(1, 4)
    assert _order_count(
        {"remaining_count": 0, "fill_count": None, "fill_count_fp": "0.25"}, "fill_count"
    ) == Decimal(".25")
    with pytest.raises(KalshiOrderConfirmationError):
        _order_count({"remaining_count": 0, "fill_count": 1, "fill_count_fp": None}, "fill_count")
    payload = build_order_body(
        ticker="T",
        side="yes",
        action="sell",
        count=Fraction(1, 4),
        yes_price=50,
        reduce_only=True,
        time_in_force="immediate_or_cancel",
    )
    assert payload["count"] == "0.25"
    assert payload["reduce_only"] is True


@pytest.mark.parametrize("engine", ["reduction", "m3"])
def test_partial_close_conserves_observed_costs_and_fee_mirrors(engine):
    from src.risk.manager import RiskManager

    buy("1.00")
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.fill_cost_usd_fp = "0.800000"
        row.fees_usd_fp = "0.010000"
        row.fill_price_cents = row.price_cents = 80
        session.add(row)
        session.commit()
    if engine == "reduction":
        persist_reduction("buy", "close", Fraction(1, 4), 80)
        finalize_reduction("close", Fraction(1, 4))
    else:
        Motor3ExitExecutor(AsyncMock())._settle_originals("T", "yes", 80, Fraction(1, 4))
    buys = [r for r in rows() if r.action == "buy"]
    remaining = next(r for r in buys if r.status == "filled")
    closed = next(r for r in buys if r.status == "settled")
    assert open_quantity(remaining) == Fraction(3, 4)
    assert remaining.fill_cost_usd_fp == "0.600000" and remaining.fees_usd_fp == "0.007500"
    assert closed.fill_cost_usd_fp == "0.200000" and closed.fees_usd_fp == "0.002500"
    assert sum(Fraction(r.fill_cost_usd_fp) for r in buys) == Fraction(".80")
    assert sum(Fraction(r.fees_usd_fp) for r in buys) == Fraction(".01")
    assert RiskManager._entry_costs(remaining, Fraction(3, 4)) == (
        Fraction(60),
        Fraction(3, 4),
        False,
    )
    assert Fraction(closed.pnl_usd_fp) == Fraction("-.0125")  # entry .25c + exit 1c


@pytest.mark.parametrize("engine", ["reduction", "m3"])
def test_final_close_consumes_entire_observed_legacy_fee(engine):
    buy("1.00")
    with models.get_session() as session:
        row = session.exec(select(models.Trade)).one()
        row.fill_price_cents = row.price_cents = 50
        row.fees_cents = 4  # Historical fee exceeds today's formula: it still was paid.
        session.add(row)
        session.commit()
    if engine == "reduction":
        persist_reduction("buy", "close", 1, 50)
        finalize_reduction("close", 1)
    else:
        Motor3ExitExecutor(AsyncMock())._settle_originals("T", "yes", 50, 1)
    closed = next(r for r in rows() if r.action == "buy")
    assert closed.pnl_cents == -6 and Fraction(closed.pnl_usd_fp) == Fraction("-.06")
    assert closed.fees_cents == 4


def test_cost_split_keeps_microdollar_residue_until_last_close():
    from src.storage.cost_allocation import allocate_entry

    buy("0.03")
    row = rows()[0]
    row.fill_cost_usd_fp = "0.012346"
    row.fees_usd_fp = "0.000001"
    row.fees_cents = 1
    allocation = allocate_entry(row, Fraction(1, 100))
    assert allocation.closed_fields["fill_cost_usd_fp"] == "0.004115"
    assert allocation.remaining_fields["fill_cost_usd_fp"] == "0.008231"
    assert allocation.closed_fields["fees_usd_fp"] == "0.000000"
    assert allocation.remaining_fields["fees_usd_fp"] == "0.000001"


async def test_m3_partial_exit_preserves_remainder_and_replay(isolated):
    buy()
    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"yes": [[60, 10]], "no": []}}
    client.place_order.return_value = {
        "order": {"remaining_count": 0, "fill_count": None, "fill_count_fp": "0.10"}
    }
    pos = models.PortfolioPosition(ticker="T", side="yes", **quantity_fields(Fraction(1, 4)))
    ex = Motor3ExitExecutor(client)
    result = await ex.exit_position(pos)
    assert result.filled_count == Fraction(1, 10)
    assert client.place_order.call_args.kwargs["count"] == Fraction(1, 4)
    # Reabrir conexión como en otro proceso: lo confirmado no vive sólo en memoria.
    isolated.dispose()
    before = rows()
    assert sorted(open_quantity(r) for r in before if r.action == "buy") == [
        Fraction(1, 10),
        Fraction(3, 20),
    ]
    residual = next(r for r in before if r.status == "filled")
    assert open_quantity(residual) == Fraction(3, 20)
    ex._finalize_fill(result.client_order_id, "T", "yes", Fraction(1, 10), 60, None)
    assert len(rows()) == len(before)
    assert sum(open_quantity(r) for r in rows() if r.action == "buy") == Fraction(1, 4)


def test_m3_crash_during_close_rolls_back_entire_transaction(monkeypatch):
    buy()
    ex = Motor3ExitExecutor(AsyncMock())
    assert ex._persist_sell_intent("sell", "T", "yes", Fraction(1, 4), 60)
    import src.strategies.motor_3_clv.executor as module

    original_resize = module.resize_filled

    def fail_on_sell(row, qty):
        if row.action == "sell":
            raise RuntimeError("crash after BUY close before SELL")
        original_resize(row, qty)

    monkeypatch.setattr(module, "resize_filled", fail_on_sell)
    with pytest.raises(RuntimeError):
        ex._finalize_fill("sell", "T", "yes", Fraction(1, 4), 60, None)
    assert {r.action: r.status for r in rows()} == {"buy": "filled", "sell": "pending"}
    monkeypatch.setattr(module, "resize_filled", original_resize)
    ex._finalize_fill("sell", "T", "yes", Fraction(1, 4), 60, None)
    assert all(r.status == "settled" for r in rows())


def test_reduction_partial_and_restart_do_not_oversell():
    buy(strategy="motor_1_arbitrage")
    persist_reduction("buy", "sell1", Fraction(1, 4), 50)
    finalize_reduction("sell1", Fraction(1, 10))
    assert open_quantity(next(r for r in rows() if r.client_order_id == "buy")) == Fraction(3, 20)
    with pytest.raises(ValueError):
        persist_reduction("buy", "too_big", Fraction(1, 4), 50)
    persist_reduction("buy", "sell2", Fraction(3, 20), 50)
    with pytest.raises(ValueError):
        persist_reduction("buy", "duplicate_pending", Fraction(3, 20), 50)
    finalize_reduction("sell2", Fraction(3, 20))
    finalize_reduction("sell2", Fraction(3, 20))
    buys = [r for r in rows() if r.action == "buy"]
    assert sum(open_quantity(r) for r in buys) == Fraction(1, 4)
    assert all(r.status == "settled" for r in buys)
    assert sum(Fraction(r.pnl_usd_fp) for r in buys) == Fraction("-0.0175")


def test_reduction_replay_rejects_different_fill_and_identity():
    buy(strategy="motor_1_arbitrage")
    persist_reduction("buy", "sell", Fraction(1, 4), 50)
    finalize_reduction("sell", Fraction(1, 4), "OID")
    with pytest.raises(ValueError, match="replay"):
        finalize_reduction("sell", Fraction(1, 10), "OID")
    with pytest.raises(ValueError, match="replay"):
        finalize_reduction("sell", Fraction(1, 4), "OTHER")


@pytest.mark.parametrize("engine", ["reduction", "m3"])
def test_terminal_sell_cannot_erase_previously_observed_fill_floor(engine):
    from src.storage.quantities import hold_uncertain_order

    buy("1.00")
    ex = Motor3ExitExecutor(AsyncMock())
    if engine == "reduction":
        persist_reduction("buy", "sell", 1, 50)
    else:
        assert ex._persist_sell_intent("sell", "T", "yes", 1, 50)
    hold_uncertain_order("sell", {"fill_count_fp": "0.25"})
    with pytest.raises(ValueError, match="regresivo"):
        if engine == "reduction":
            finalize_reduction("sell", Fraction(1, 10))
        else:
            ex._finalize_fill("sell", "T", "yes", Fraction(1, 10), 50, None)
    assert next(r for r in rows() if r.action == "sell").status == "pending"
    assert open_quantity(next(r for r in rows() if r.action == "buy")) == 1


async def test_rest_reconcile_regressive_zero_keeps_fill_floor_and_never_sells():
    client = AsyncMock()
    coids = []

    async def place(**kwargs):
        coids.append(kwargs["client_order_id"])
        return {
            "order": {"status": "resting", "fill_count_fp": "0.25", "remaining_count_fp": "4.75"}
        }

    async def orders(**kwargs):
        return {
            "orders": [{"client_order_id": coids[0], "status": "canceled", "fill_count_fp": "0.00"}]
        }

    client.place_order.side_effect = place
    client.get_orders.side_effect = orders
    result = await RestExecutor(client).execute(_opp())
    assert result.kill_switch_fired and not result.rollback_triggered
    assert client.place_order.await_count == 1
    pending = next(r for r in rows() if r.status == "pending")
    assert pending.filled_count_fp == "0.25" and requested_quantity(pending) == 5


async def test_restart_after_split_never_restores_original_cumulative_fill():
    buy(strategy="motor_1_arbitrage")
    persist_reduction("buy", "sell1", Fraction(1, 4), 50)
    finalize_reduction("sell1", Fraction(1, 10), "S1")
    persist_reduction("buy", "sell2", Fraction(3, 20), 50)
    with models.get_session() as s:
        t = s.exec(select(models.Trade).where(models.Trade.client_order_id == "sell2")).one()
        t.placed_at = datetime.now(UTC) - timedelta(minutes=2)
        s.add(t)
        s.commit()
    client = AsyncMock()
    client.get_orders.return_value = {
        "orders": [
            {
                "client_order_id": "buy",
                "order_id": "B",
                "status": "executed",
                "fill_count_fp": "0.25",
            },
            {
                "client_order_id": "sell1",
                "order_id": "S1",
                "status": "canceled",
                "fill_count_fp": "0.10",
            },
            {
                "client_order_id": "sell2",
                "order_id": "S2",
                "status": "executed",
                "fill_count_fp": "0.15",
            },
        ]
    }
    await reconcile_pending_orders(client, strategies=("motor_1_arbitrage",))
    await reconcile_pending_orders(client, strategies=("motor_1_arbitrage",))
    buys = [r for r in rows() if r.action == "buy"]
    assert all(r.status == "settled" for r in buys)
    assert sum(open_quantity(r) for r in buys) == Fraction(1, 4)
    assert len(rows()) == 4


async def test_restart_reconciles_cancelled_order_with_fraction_instead_of_zero():
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id="ioc",
                ticker="T",
                side="yes",
                action="buy",
                strategy="motor_2_consensus",
                price_cents=45,
                placed_at=datetime.now(UTC) - timedelta(minutes=2),
                **quantity_fields(6),
            )
        )
        s.commit()
    client = AsyncMock()
    client.get_orders.return_value = {
        "orders": [
            {
                "client_order_id": "ioc",
                "order_id": "OID",
                "status": "canceled",
                "fill_count": None,
                "fill_count_fp": "0.25",
            }
        ]
    }
    await reconcile_pending_orders(client, strategies=("motor_2_consensus",))
    (row,) = rows()
    assert row.status == "filled" and open_quantity(row) == Fraction(1, 4)


async def test_rest_fok_partial_only_rolls_back_confirmed_remainders():
    client = AsyncMock()
    client.place_order.side_effect = [
        {"order": {"remaining_count": 0, "fill_count": None, "fill_count_fp": n}}
        for n in ("0.25", "0.10", "0.15")
    ]
    outcome = await RestExecutor(client).execute(_opp())
    assert not outcome.filled and outcome.rollback_filled
    calls = [c.kwargs for c in client.place_order.call_args_list]
    assert [c["action"] for c in calls] == ["buy", "sell", "sell"]
    assert [c["count"] for c in calls[1:]] == [Fraction(1, 4), Fraction(3, 20)]
    buys = [r for r in rows() if r.action == "buy" and r.status == "settled"]
    assert sum(open_quantity(r) for r in buys) == Fraction(1, 4)


def test_portfolio_cache_keeps_quarter_contract():
    p = PortfolioPoller()
    p._persist({"T": ("no", Fraction(1, 4), 12)}, {})
    with models.get_session() as s:
        row = s.exec(select(models.PortfolioPosition)).one()
        assert requested_quantity(row) == Fraction(1, 4)
        assert row.count_fp == "0.25" and row.side == "no"


def test_legacy_filled_fallback_never_applies_to_new_or_pending_rows():
    row = models.Trade(
        client_order_id="old",
        ticker="T",
        side="yes",
        action="buy",
        strategy="motor_2_consensus",
        price_cents=45,
        count=2,
        status="filled",
    )
    assert filled_quantity(row) == 2  # Convención histórica, no nueva confirmación de API.
    row.count_fp = "2.00"
    assert filled_quantity(row) is None
    row.count_fp = None
    row.status = "pending"
    assert filled_quantity(row) is None
    row.status = "filled"
    row.strategy = "motor_5_mm"
    assert filled_quantity(row) is None


async def test_fractional_hedge_excluded_and_orphan_cap_exact():
    for side in ("yes", "no"):
        row = models.Trade(
            client_order_id=f"hedge-{side}",
            ticker="T",
            side=side,
            action="buy",
            strategy="motor_1_arbitrage",
            price_cents=40,
            status="filled",
            notes="arb_id=11111111-1111-1111-1111-111111111111",
            **filled_fields(Fraction(1, 4)),
        )
        with models.get_session() as s:
            s.add(row)
            s.commit()
    buy("0.10", coid="orphan", strategy="motor_1_arbitrage")
    with models.get_session() as s:
        orphan = s.exec(select(models.Trade).where(models.Trade.client_order_id == "orphan")).one()
        orphan.notes = "arb_id=22222222-2222-2222-2222-222222222222"
        s.add(orphan)
        s.commit()
    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"yes": [[60, 10]], "no": []}}
    client.place_order.return_value = {"order": {"remaining_count": 0, "fill_count_fp": "0.10"}}
    ex = Motor3ExitExecutor(client, include_motor1_orphans=True)
    result = await ex.exit_position(
        models.PortfolioPosition(ticker="T", side="yes", **quantity_fields(Fraction("0.35")))
    )
    assert result.filled_count == Fraction("0.10")
    assert client.place_order.call_args.kwargs["count"] == Fraction("0.10")
    assert all(
        r.status == "filled" and open_quantity(r) == Fraction("0.25")
        for r in rows()
        if r.client_order_id.startswith("hedge")
    )
