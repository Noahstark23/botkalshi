"""Flujos async de salidas con exchange ficticio y SQLite; sin red ni flags live.

Ejercita los callers M1/M3/REST y recovery compartido, no selección de mercados
ni runner completo. El fake sólo simula una respuesta ya definida por la prueba.
"""

import asyncio
from fractions import Fraction
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
from src.math.arbitrage import ArbLeg
from src.math.quantities import fixed_count
from src.monitoring.health import BotState
from src.storage.order_reconciliation import (
    capture_terminal_sell_execution,
    reconcile_pending_orders,
)
from src.storage.quantities import confirmed_fill, filled_fields, requested_quantity
from src.storage.reductions import finalize_reduction, persist_reduction
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor
from src.strategies.motor_rest_arb.executor import RestExecutor
from src.utils.config import Settings

TICKER = "OFFLINE-ASYNC-EXIT"


@pytest.fixture(autouse=True)
def lab(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'async-exits.db'}")
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    monkeypatch.setattr(
        "src.strategies.motor_1_arbitrage.executor.get_settings", lambda: Settings.model_construct()
    )
    for target in (
        "socket.socket.connect",
        "socket.socket.connect_ex",
        "socket.getaddrinfo",
        "socket.create_connection",
        "src.clients.kalshi_rest.KalshiRestClient.__init__",
    ):
        monkeypatch.setattr(target, Mock(side_effect=AssertionError("OFFLINE_ONLY")))
    for target in (
        "src.monitoring.telegram_alerts.send_alert",
        "src.strategies.motor_1_arbitrage.executor.alert_error",
        "src.strategies.motor_1_arbitrage.executor.alert_risk_event",
    ):
        monkeypatch.setattr(target, AsyncMock())
    yield
    engine.dispose()


def ledger():
    with models.get_session() as session:
        return list(session.exec(select(models.Trade).order_by(models.Trade.id)))


def seed(strategy="motor_2_consensus", side="yes"):
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="buy",
                ticker=TICKER,
                side=side,
                action="buy",
                strategy=strategy,
                price_cents=40,
                status="filled",
                fill_cost_usd_fp="0.400000",
                fees_usd_fp="0.002000",
                fees_cents=1,
                commitment_origin_id="original-commitment",
                **filled_fields(1),
            )
        )
        session.commit()


class Exchange:
    def __init__(self, *, side="yes", sold=Fraction(1), requested=Fraction(1), fail=None):
        self.side, self.sold, self.fail = side, sold, fail
        self.requested = requested
        self.orders = []
        self.posts = []
        self.reads = []
        self.before_fill_get = []
        self.waiting = asyncio.Event()

    def order(self, coid):
        result = {
            "order_id": "order-exit",
            "client_order_id": coid,
            "ticker": TICKER,
            "side": self.side,
            "action": "sell",
            "outcome_side": "no" if self.side == "yes" else "yes",
            "book_side": "ask" if self.side == "yes" else "bid",
            "subaccount_number": 0,
            "fill_count_fp": fixed_count(self.sold),
            "initial_count_fp": fixed_count(self.requested),
            "remaining_count_fp": "0.00",
            "status": "canceled",
            # No son ingresos SELL y su semántica no debe contaminar la captura.
            "maker_fill_cost_dollars": "0.000000",
            "taker_fill_cost_dollars": "NaN",
        }
        if self.fail == "missing_oid":
            del result["order_id"]
        return result

    async def place_order(self, **kwargs):
        self.posts.append(kwargs)
        assert kwargs["action"] == "sell" and kwargs["reduce_only"] is True
        assert kwargs["time_in_force"] == "immediate_or_cancel"
        row = next(r for r in ledger() if r.client_order_id == kwargs["client_order_id"])
        assert row.status == "pending" and row.kalshi_order_id is None
        order = self.order(kwargs["client_order_id"])
        self.orders = [order]
        return {"order": order}

    async def get_orderbook(self, ticker, **_):
        assert ticker == TICKER
        return {"orderbook": {"yes": [[60, 100]], "no": [[60, 100]]}}

    async def get_orders(self, **_):
        return {"orders": self.orders, "cursor": ""}

    async def get_fills(self, **kwargs):
        self.reads.append("live")
        row = next(r for r in ledger() if r.action == "sell" and r.status == "pending")
        # Nueva conexión/sesión demuestra commit real ANTES de cualquier await.
        assert row.kalshi_order_id == kwargs["order_id"] == "order-exit"
        assert confirmed_fill(row) == self.sold and requested_quantity(row) == self.requested
        self.before_fill_get.append(row.model_dump())
        if self.fail == "cancel_await":
            self.waiting.set()
            await asyncio.Event().wait()
        if self.fail == "live":
            raise RuntimeError("fixture live unavailable")
        record = {
            "fill_id": "fill-exit",
            "order_id": "order-exit",
            "ticker": TICKER,
            "side": self.side,
            "action": "sell",
            "count_fp": fixed_count(self.sold),
            f"{self.side}_price_dollars": "0.700100",
            "fee_cost": "0.001234",
            "subaccount_number": 0,
        }
        return {"fills": [record] if self.fail != "empty" else [], "cursor": ""}

    async def get_historical_fills(self, **kwargs):
        self.reads.append("historical")
        assert "order_id" not in kwargs
        if self.fail == "historical":
            raise RuntimeError("fixture historical unavailable")
        return {"fills": [], "cursor": ""}


async def execute(mode, client, *, side="yes"):
    leg = ArbLeg(TICKER, side, 40, 1, 100)
    if mode == "m1":
        ex = ArbitrageExecutor(client, object(), max_rollback_retries=1)
        await ex._execute_iterative_rollback([(leg, "buy", 1)])
        return ex
    if mode == "rest":
        ex = RestExecutor(client)
        ex.ROLLBACK_MAX_RETRIES = 1
        return await ex._sell_to_exit(leg, "buy")
    return await Motor3ExitExecutor(client).exit_position(
        models.PortfolioPosition(ticker=TICKER, side=side, count=1)
    )


@pytest.mark.parametrize(
    "mode,strategy",
    [("m1", "motor_1_arbitrage"), ("rest", "motor_rest_arb"), ("m3", "motor_2_consensus")],
)
@pytest.mark.parametrize("side", ["yes", "no"])
async def test_real_async_callers_use_observed_price_and_fee(mode, strategy, side):
    seed(strategy, side)
    client = Exchange(side=side)
    outcome = await execute(mode, client, side=side)
    assert len(client.posts) == 1 and client.reads == ["live", "historical"]
    buy, sell = ledger()
    assert buy.status == sell.status == "settled"
    assert buy.pnl_usd_fp == "0.296866"  # .700100-.400000-.002000-.001234
    assert sell.sale_proceeds_usd_fp == "0.700100" and sell.fees_usd_fp == "0.001234"
    assert sell.fill_price_cents is None and sell.fill_cost_usd_fp is None
    assert "exit_execution_observed" in buy.notes
    assert not models.kill_switch_engaged()[0]
    if mode == "m3":
        assert outcome.filled and outcome.sell_price_cents is None and not outcome.reason
    elif mode == "rest":
        assert outcome is True


@pytest.mark.parametrize(
    "mode,strategy",
    [("m1", "motor_1_arbitrage"), ("rest", "motor_rest_arb"), ("m3", "motor_2_consensus")],
)
@pytest.mark.parametrize("failure", ["live", "historical", "empty", "missing_oid"])
async def test_capture_failure_retains_pending_fill_and_blocks_duplicate_sell(
    mode, strategy, failure
):
    seed(strategy)
    client = Exchange(fail=failure)
    outcome = await execute(mode, client)
    buy, sell = ledger()
    assert buy.status == "filled" and buy.pnl_usd_fp is None
    assert sell.status == "pending" and confirmed_fill(sell) == 1 and requested_quantity(sell) == 1
    assert sell.kalshi_order_id == (None if failure == "missing_oid" else "order-exit")
    assert sell.sale_proceeds_usd_fp is None and sell.exit_fill_evidence_sha256 is None
    assert BotState.is_paused and models.kill_switch_engaged()[0]
    if mode == "m3":
        assert outcome.filled and outcome.reason == "exit_accounting_pending"
        assert outcome.sell_price_cents is None
    await execute(mode, client)
    assert len(client.posts) == 1


@pytest.mark.parametrize(
    "mode,strategy",
    [("m1", "motor_1_arbitrage"), ("rest", "motor_rest_arb"), ("m3", "motor_2_consensus")],
)
async def test_zero_terminal_fill_does_not_fetch_execution_evidence(mode, strategy):
    seed(strategy)
    client = Exchange(sold=Fraction(0))
    await execute(mode, client)
    assert len(client.posts) == 1 and client.reads == []
    buy, sell = ledger()
    assert buy.status == "filled" and sell.status == "cancelled"


@pytest.mark.parametrize("strategy", ["motor_1_arbitrage", "motor_rest_arb", "motor_2_consensus"])
async def test_shared_recovery_requires_fills_and_resumes_exactly_once(strategy):
    seed(strategy)
    persist_reduction("buy", "sell", 1, 60)
    client = Exchange(sold=Fraction(1, 4), fail="historical")
    client.orders = [client.order("sell")]
    first = await reconcile_pending_orders(client, strategies=(strategy,))
    assert first.unresolved == ("sell",) and first.resolved == 0
    buy, sell = ledger()
    assert sell.status == "pending" and confirmed_fill(sell) == Fraction(1, 4)
    assert sell.kalshi_order_id == "order-exit" and buy.status == "filled"
    assert models.kill_switch_engaged()[0]
    client.fail = None
    second = await reconcile_pending_orders(client, strategies=(strategy,))
    assert second.resolved == 1 and not second.unresolved
    closed = next(r for r in ledger() if r.action == "buy" and r.status == "settled")
    assert closed.pnl_usd_fp == "0.073291"  # .175025-.100000-.000500-.001234
    before = [r.model_dump() for r in ledger()]
    third = await reconcile_pending_orders(client, strategies=(strategy,))
    assert third.checked == 0 and [r.model_dump() for r in ledger()] == before
    assert not client.posts and models.kill_switch_engaged()[0]


async def test_m3_restart_recovery_captures_exact_fills_without_another_post():
    seed()
    client = Exchange(fail="historical")
    await execute("m3", client)
    client.fail = None
    restarted = Motor3ExitExecutor(client)
    assert await restarted._reconcile_pending_sells(TICKER, "yes")
    assert len(client.posts) == 1
    assert all(r.status == "settled" for r in ledger())
    assert next(r for r in ledger() if r.action == "buy").pnl_usd_fp == "0.296866"
    assert models.kill_switch_engaged()[0]  # Capturar dinero no levanta la pausa.


async def test_cancellation_during_get_preserves_durable_floor_and_pause():
    seed()
    persist_reduction("buy", "sell", 1, 60)
    client = Exchange(fail="cancel_await")
    task = asyncio.create_task(
        capture_terminal_sell_execution(client, "sell", client.order("sell"))
    )
    await asyncio.wait_for(client.waiting.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    buy, sell = ledger()
    assert buy.status == "filled" and sell.status == "pending"
    assert sell.kalshi_order_id == "order-exit" and confirmed_fill(sell) == 1
    assert models.kill_switch_engaged()[0] and BotState.is_paused


async def test_wrong_ack_identity_is_not_attributed_and_existing_quarantine_is_retained():
    seed()
    persist_reduction("buy", "sell", 1, 60)
    client = Exchange()
    wrong = client.order("another-coid")
    with pytest.raises(ValueError):
        await capture_terminal_sell_execution(client, "sell", wrong)
    assert confirmed_fill(ledger()[1]) is None and not client.reads
    with models.get_session() as session:
        sell = session.exec(
            select(models.Trade).where(models.Trade.client_order_id == "sell")
        ).one()
        sell.notes += " fill_cost_quarantined"
        session.add(sell)
        session.commit()
    with pytest.raises(ValueError, match="COSTS_QUARANTINED"):
        await capture_terminal_sell_execution(client, "sell", client.order("sell"))
    assert "fill_cost_quarantined" in ledger()[1].notes
    assert confirmed_fill(ledger()[1]) == 1 and not client.reads


async def test_partial_fee_keeps_original_quantity_until_larger_terminal_capture():
    seed()
    with models.get_session() as session:
        buy = session.exec(select(models.Trade)).one()
        for key, value in filled_fields(2).items():
            setattr(buy, key, value)
        buy.fill_cost_usd_fp = "0.800000"
        buy.fees_usd_fp = "0.004000"
        session.add(buy)
        session.commit()
    persist_reduction("buy", "sell", 2, 60)
    with models.get_session() as session:
        sell = session.exec(
            select(models.Trade).where(models.Trade.client_order_id == "sell")
        ).one()
        sell.filled_count = 1
        sell.filled_count_fp = "1.00"
        sell.fees_usd_fp = "0.000500"
        sell.fees_cents = 1
        session.add(sell)
        session.commit()
    client = Exchange(sold=Fraction(2), requested=Fraction(2))
    summary = await capture_terminal_sell_execution(client, "sell", client.order("sell"))
    before_get = client.before_fill_get[0]
    assert before_get["notes"] == "reduction_for=buy"
    assert before_get["fees_usd_fp"] is None
    assert before_get["observed_fee_floor_usd_fp"] == "0.000500"
    assert before_get["observed_fee_floor_count_fp"] == "1.00"
    assert before_get["filled_count_fp"] == "2.00"
    finalize_reduction("sell", Fraction(2), "order-exit", execution=summary)
    buy, sell = ledger()
    assert buy.status == sell.status == "settled"
    assert buy.pnl_usd_fp == "0.594966"
    assert sell.fees_usd_fp == "0.001234"
    assert sell.observed_fee_floor_count_fp == "2.00"
    assert not models.kill_switch_engaged()[0]
