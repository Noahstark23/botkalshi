"""Recorridos offline reales: ejecución, SQLite, fills parciales y recuperación.

El transporte es ficticio y cada DB es temporal. Los BUY conservan estimaciones
legacy; los SELL aportan fills GET sintéticos explícitos (44c, fee 1c por orden).
No es evidencia del proveedor ni de rentabilidad. No se construye cliente HTTP.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from unittest.mock import AsyncMock, Mock

import pytest
from freezegun import freeze_time
from sqlmodel import create_engine, select

import src.storage.models as models
import src.utils.config as config
from src.math.arbitrage import ArbLeg, ArbOpportunity, detect_binary_arb
from src.math.quantities import fixed_count
from src.monitoring.health import BotState
from src.risk.commitments import daily_committed_cents
from src.risk.exposure import open_risk_cents
from src.risk.manager import RiskManager
from src.storage.order_reconciliation import reconcile_pending_orders
from src.storage.quantities import confirmed_fill, open_quantity, requested_quantity
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.strategies.motor_5_mm.incidents import incident_status
from src.strategies.motor_5_mm.quoter import QuoteSet
from src.strategies.motor_5_mm.reconciler import MMReconciler
from src.strategies.motor_rest_arb.executor import RestExecutor
from src.strategies.motor_rest_arb.settlement import MarketResolution, SettlementPoller

NOW = datetime(2026, 10, 6, 16, tzinfo=UTC)
START = NOW - timedelta(hours=1)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """No .env, credentials, network or persistent bot database in these tests."""
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith(("KALSHI_", "ODDS_", "TELEGRAM_", "JEV_")):
            monkeypatch.delenv(key)
    denied = Mock(side_effect=AssertionError("offline integration: network forbidden"))
    for target in (
        "socket.socket.connect",
        "socket.socket.connect_ex",
        "socket.create_connection",
        "socket.getaddrinfo",
    ):
        monkeypatch.setattr(target, denied)
    monkeypatch.setattr("src.clients.kalshi_rest.KalshiRestClient.__init__", denied)
    engine = create_engine(f"sqlite:///{tmp_path}/night.db")
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    settings = config.Settings.model_construct(
        KALSHI_API_KEY_ID="",
        KALSHI_PRIVATE_KEY_PATH="",
        KALSHI_ENV="demo",
        DATABASE_URL=f"sqlite:///{tmp_path}/night.db",
        TRADING_ENABLED=True,  # Exercise real executor; only our fake has place_order.
        ACTIVE_CAPITAL_USD=200,
        CAPITAL_CAP_USD=200,
        CAPITAL_FLOOR_USD=180,
        CAPITAL_SAFETY_FACTOR_PCT=90,
        CAPITAL_SMOOTHING_PCT=0,
        DYNAMIC_CAPITAL_ENABLED=True,
        MAX_TRADE_SIZE_USD=2,
        MAX_TRADE_SIZE_PCT=1,
        MAX_SIMULTANEOUS_EXPOSURE_PCT=3,
        MAX_EVENT_DIRECTIONAL_EXPOSURE_USD=2,
        EXPERIMENT_BANK200_ENABLED=True,
        EXPERIMENT_START_AT=START.isoformat(),
        UNREALIZED_STOP_ENABLED=False,
    )
    monkeypatch.setattr(config, "_settings", settings)
    monkeypatch.setattr(RiskManager, "_check_lock", asyncio.Lock())
    monkeypatch.setattr(RiskManager, "_marks", {})
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", 200.0)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 200.0)
    monkeypatch.setattr(RiskManager, "_last_balance_at", NOW.replace(tzinfo=None))
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    monkeypatch.setattr("src.monitoring.telegram_alerts.send_alert", AsyncMock())
    monkeypatch.setattr("src.strategies.motor_1_arbitrage.executor.alert_error", AsyncMock())
    monkeypatch.setattr("src.strategies.motor_1_arbitrage.executor.alert_risk_event", AsyncMock())
    monkeypatch.setattr(Motor5Executor, "_locks", {})
    with freeze_time(NOW, real_asyncio=True):
        yield settings, engine
    engine.dispose()
    denied.assert_not_called()


def trades():
    with models.get_session() as session:
        return list(session.exec(select(models.Trade).order_by(models.Trade.id)))


def daily():
    with models.get_session() as session:
        return daily_committed_cents(session, start=START, now=NOW)


def binary():
    opportunity = detect_binary_arb("EVENT-A", 40, 10, 45, 10, max_count=1)
    assert opportunity is not None
    return opportunity


def three_outcome():
    return ArbOpportunity(
        legs=tuple(
            ArbLeg(f"REST-EVENT-{side}", "yes", price, 1, 100)
            for side, price in (("HOME", 45), ("DRAW", 25), ("AWAY", 20))
        ),
        count=1,
        gross_profit_cents=10,
        fees_cents=6,
        net_profit_cents=4,
        edge_pct=4,
    )


class PartialTransport:
    """Acknowledgments preserve identity; sequence may lose an IOC response."""

    def __init__(self, *, lost_second_sell=False):
        self.calls = []
        self.lost_second_sell = lost_second_sell
        self.orders = {}

    async def get_available_balance_usd(self):
        return 200.0

    async def get_orderbook(self, ticker, **kwargs):
        return {"orderbook": {"yes": [[44, 10]], "no": [[44, 10]]}}

    async def place_order(self, **kwargs):
        # Intent and reservation must exist before this simulated network boundary.
        row = next(t for t in trades() if t.client_order_id == kwargs["client_order_id"])
        assert row.status == "pending" and requested_quantity(row) == kwargs["count"]
        self.calls.append(kwargs)
        if kwargs["action"] == "buy":
            fill = Fraction(3, 4)
        else:
            assert kwargs["reduce_only"] and kwargs["time_in_force"] == "immediate_or_cancel"
            sell_number = len([c for c in self.calls if c["action"] == "sell"])
            if sell_number == 2 and self.lost_second_sell:
                raise TimeoutError("simulated accepted IOC; response lost")
            fill = Fraction(1, 4) if sell_number == 1 else Fraction(1, 2)
        order = self.observation(kwargs, fill, status="canceled", remaining=0)
        self.orders[kwargs["client_order_id"]] = order
        return {"order": order}

    @staticmethod
    def observation(call, fill, *, status, remaining):
        return {
            "order_id": f"fake-{call['client_order_id']}",
            "client_order_id": call["client_order_id"],
            "ticker": call["ticker"],
            "side": call["side"],
            "action": call["action"],
            "initial_count_fp": fixed_count(call["count"]),
            "fill_count_fp": fixed_count(fill),
            "remaining_count_fp": fixed_count(remaining),
            "status": status,
        }

    async def get_orders(self, **kwargs):
        return {"cursor": "", "orders": list(self.orders.values())}

    async def get_fills(self, *, order_id, ticker, subaccount, limit, **kwargs):
        assert subaccount == 0 and limit == 100 and not kwargs
        order = next(o for o in self.orders.values() if o["order_id"] == order_id)
        assert order["ticker"] == ticker and order["action"] == "sell"
        return {
            "fills": [
                {
                    "fill_id": f"fill-{order_id}",
                    "order_id": order_id,
                    "ticker": ticker,
                    "side": order["side"],
                    "action": "sell",
                    "subaccount_number": 0,
                    "count_fp": order["fill_count_fp"],
                    f"{order['side']}_price_dollars": "0.44",
                    "fee_cost": "0.01",
                }
            ],
            "cursor": "",
        }

    async def get_historical_fills(self, **kwargs):
        assert "order_id" not in kwargs
        return {"fills": [], "cursor": ""}


async def test_m1_partial_roundtrip_keeps_original_budget_after_restart(sandbox):
    client = PartialTransport()
    risk = RiskManager()
    assert not await ArbitrageExecutor(client, risk).execute(binary())
    assert [c["action"] for c in client.calls] == ["buy", "sell", "sell"]
    assert [c["count"] for c in client.calls] == [1, Fraction(3, 4), Fraction(1, 2)]
    rows = trades()
    closed = [t for t in rows if t.action == "buy" and t.status == "settled"]
    assert sum((confirmed_fill(t) for t in closed), Fraction()) == Fraction(3, 4)
    assert sum(t.fees_cents for t in closed) == 2
    assert sum(t.fees_cents for t in rows if t.action == "sell") == 2
    # Entrada legacy estimada; salida GET sintética de .75 a 44c, dos fees de 1c.
    assert sum((Fraction(t.pnl_usd_fp) for t in closed), Fraction()) == Fraction(-475, 10000)
    assert sum(t.pnl_cents for t in closed) == -6  # Per-row floor is conservative.
    assert risk._get_current_exposure_usd() == 0
    assert daily() == 89  # Both original requested legs + fee reserves, never refunded.
    with models.get_session() as session:
        commitments = list(session.exec(select(models.RiskCommitment)))
    assert len(commitments) == 2
    assert len({t.commitment_origin_id for t in closed}) == 1

    sandbox[1].dispose()
    restarted = RiskManager()
    assert restarted._get_current_exposure_usd() == 0 and daily() == 89
    recovery = await reconcile_pending_orders(client, strategies=("motor_1_arbitrage",))
    assert recovery.checked == recovery.resolved == 0
    source = AsyncMock()
    source.get_resolution.return_value = MarketResolution("EVENT-A", "no")
    await SettlementPoller(source).settle_once()
    source.get_resolution.assert_not_called()  # Closed rollback legs never settle twice.
    assert daily() == 89 and len(trades()) == len(rows)
    assert len(client.calls) == 3


@pytest.mark.parametrize("motor", ["m1", "rest"])
async def test_restart_retains_late_sell_fill_floor_before_regressive_cancel(sandbox, motor):
    client = PartialTransport(lost_second_sell=True)
    risk = RiskManager()
    if motor == "m1":
        assert not await ArbitrageExecutor(client, risk).execute(binary())
        strategy = "motor_1_arbitrage"
        assert daily() == 89
    else:
        sandbox[0].EXPERIMENT_BANK200_ENABLED = False
        opportunity = three_outcome()
        assert (await risk.check_pre_trade(opportunity)).approved
        outcome = await RestExecutor(client).execute(opportunity)
        assert outcome.rollback_triggered and outcome.kill_switch_fired
        strategy = "motor_rest_arb"
        with models.get_session() as session:
            assert not list(session.exec(select(models.RiskCommitment)))
    assert [c["action"] for c in client.calls] == ["buy", "sell", "sell"]
    pending = next(t for t in trades() if t.action == "sell" and t.status == "pending")
    assert requested_quantity(pending) == Fraction(1, 2)
    assert confirmed_fill(pending) is None
    assert models.kill_switch_engaged()[0]
    post_timeout_decision = await RiskManager().check_pre_trade(binary())
    call = client.calls[-1]
    sandbox[1].dispose()  # Recovery uses only persisted rows, no executor local state.
    client.orders[pending.client_order_id] = client.observation(
        call, Fraction(1, 4), status="resting", remaining=Fraction(1, 4)
    )
    first = await reconcile_pending_orders(client, strategies=(strategy,))
    after_late_fill = next(t for t in trades() if t.id == pending.id)
    assert first.unresolved == (pending.client_order_id,)
    assert after_late_fill.status == "pending"
    assert requested_quantity(after_late_fill) == Fraction(1, 2)
    sandbox[1].dispose()
    client.orders[pending.client_order_id] = client.observation(
        call, 0, status="canceled", remaining=0
    )
    regressive = await reconcile_pending_orders(client, strategies=(strategy,))
    current = next(t for t in trades() if t.id == pending.id)
    print(
        json.dumps(
            {
                "motor": motor,
                "kill_switch_durable": models.kill_switch_engaged()[0],
                "post_timeout_approved": post_timeout_decision.approved,
                "late_fill_fp": after_late_fill.filled_count_fp,
                "after_regressive_status": current.status,
                "after_regressive_fill_fp": current.filled_count_fp,
                "regressive_resolved": regressive.resolved,
                "network_placements": len(client.calls),
            },
            sort_keys=True,
        )
    )
    assert not post_timeout_decision.approved
    # Preserve the observed minimum BEFORE another GET can contradict it.
    assert confirmed_fill(after_late_fill) == Fraction(1, 4)
    assert regressive.unresolved == (pending.client_order_id,) and regressive.resolved == 0
    assert current.status == "pending" and confirmed_fill(current) == Fraction(1, 4)
    assert len(client.calls) == 3 and models.kill_switch_engaged()[0]

    # A different exchange order cannot close this intent even with a plausible fill.
    terminal = client.observation(call, Fraction(1, 4), status="canceled", remaining=0)
    client.orders[pending.client_order_id] = {**terminal, "order_id": "foreign-order"}
    foreign = await reconcile_pending_orders(client, strategies=(strategy,))
    assert foreign.unresolved == (pending.client_order_id,)
    assert next(t for t in trades() if t.id == pending.id).status == "pending"

    # Valid terminal evidence reduces only the attributed BUY; replay never resells
    # or allocates its entry fees twice, and the original commitment is unchanged.
    sandbox[1].dispose()
    client.orders[pending.client_order_id] = terminal
    complete = await reconcile_pending_orders(client, strategies=(strategy,))
    assert complete.resolved == 1 and not complete.unresolved
    opened = [t for t in trades() if t.action == "buy" and t.status == "filled"]
    assert len(opened) == 1 and open_quantity(opened[0]) == Fraction(1, 4)
    assert next(t for t in trades() if t.id == pending.id).status == "settled"
    rows_before_replay = trades()
    replay = await reconcile_pending_orders(client, strategies=(strategy,))
    assert replay.checked == 0 and len(trades()) == len(rows_before_replay)
    assert len(client.calls) == 3 and models.kill_switch_engaged()[0]
    if motor == "m1":
        assert daily() == 89


class QuoteTransport:
    def __init__(self):
        self.order = None
        self.cancel_fails = True
        self.placements = 0
        self.cancels = 0

    async def place_order(self, **kwargs):
        self.placements += 1
        self.order = PartialTransport.observation(kwargs, 0, status="resting", remaining=1)
        self.costs("0.00", "0.000000", "0.000000")
        return {"order": dict(self.order)}

    def costs(self, fill, gross, fee):
        self.order.update(
            fill_count_fp=fill,
            remaining_count_fp=fixed_count(1 - Fraction(fill)),
            maker_fill_cost_dollars=gross,
            taker_fill_cost_dollars="0.000000",
            maker_fees_dollars=fee,
            taker_fees_dollars="0.000000",
        )

    async def cancel_order(self, order_id):
        assert order_id == self.order["order_id"]
        self.cancels += 1
        if self.cancel_fails:
            raise TimeoutError("simulated cancel response lost")
        self.order.update(status="canceled", remaining_count_fp="0.00")
        return {"order": dict(self.order)}

    async def get_all_orders(self, **kwargs):
        return [dict(self.order)]


async def test_cancel_timeout_then_late_fractional_fill_survives_restart(sandbox):
    sandbox[0].EXPERIMENT_BANK200_ENABLED = False
    client = QuoteTransport()
    risk = RiskManager()
    executor = Motor5Executor(client, risk=risk)
    quote = QuoteSet("MM-A", 0.5, 47, None, 1)
    assert await executor.sync_quotes(quote) == "synced"
    (original,) = trades()
    assert open_risk_cents(original) == 47 and confirmed_fill(original) == 0
    await executor.retire_ticker("MM-A")
    assert trades()[0].status == "pending" and open_risk_cents(trades()[0]) == 47
    assert incident_status()[0]
    sandbox[1].dispose()

    client.costs("0.25", "0.107500", "0.000125")
    first = await MMReconciler(client).reconcile(live_coids=set())
    row = trades()[0]
    assert not first.complete and row.status == "pending"
    assert confirmed_fill(row) == Fraction(1, 4) and open_risk_cents(row) == 47
    assert row.fill_cost_usd_fp == "0.107500" and row.fees_usd_fp == "0.000125"
    assert row.fees_cents == 1  # Exact FP retained; integer compatibility rounds upward.
    sandbox[1].dispose()

    client.cancel_fails = False
    second = await MMReconciler(client).reconcile(live_coids=set())
    row = trades()[0]
    assert second.complete and second.orphan_cancelled == 1
    assert row.status == "filled" and open_quantity(row) == Fraction(1, 4)
    assert open_risk_cents(row) == 12
    assert incident_status()[0]  # Recovery does not erase a durable incident.
    restarted = RiskManager()
    restarted.record_mark("MM-A", "yes", 40, ask_cents=41, observed_at=NOW, fee_multiplier=0.5)
    snapshot = restarted._unrealized_snapshot()
    assert snapshot["unknown"] == []
    assert snapshot["pnl_cents"] == Fraction(-141, 80)  # -1.7625c, incl close fee 1c.
    assert not (await restarted.check_pre_trade(binary())).approved
    third = await MMReconciler(client).reconcile(live_coids=set())
    assert third.pending_checked == 0 and len(trades()) == 1
    assert client.placements == 1 and client.cancels == 3
