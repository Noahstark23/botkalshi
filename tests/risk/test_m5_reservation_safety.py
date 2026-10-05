"""Riesgo M5 con SQLite desechable: ningún cliente de red ni credencial real."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlmodel import SQLModel, create_engine, select

import src.storage.models as models
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.math.fees import kalshi_fee_cents
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.storage.models import OperationalState, Trade, get_session
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.strategies.motor_5_mm.incidents import INCIDENT_KEY, incident_status, latch_incident
from src.strategies.motor_5_mm.order_state import COUNT_QUARANTINE_NOTE
from src.strategies.motor_5_mm.quoter import QuoteSet
from src.utils.config import Settings


@pytest.fixture
def risk(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'm5-reservation.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    settings = Settings.model_construct(
        KALSHI_API_KEY_ID="offline-test-key",
        TRADING_ENABLED=False,
        ACTIVE_CAPITAL_USD=200,
        CAPITAL_CAP_USD=200,
        CAPITAL_FLOOR_USD=100,
        CAPITAL_SAFETY_FACTOR_PCT=100,
    )
    monkeypatch.setattr("src.risk.manager.get_settings", lambda: settings)
    monkeypatch.setattr(RiskManager, "_check_lock", asyncio.Lock())
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", 200.0)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 200.0)
    monkeypatch.setattr(RiskManager, "_last_balance_at", datetime.now(UTC).replace(tzinfo=None))
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    Motor5Executor._locks.clear()
    yield RiskManager()
    engine.dispose()
    Motor5Executor._locks.clear()


class FakeClient:
    def __init__(self):
        self.placed = []

    async def place_order(self, **kwargs):
        # La reserva debe estar persistida antes del primer await externo.
        with get_session() as session:
            row = session.exec(
                select(Trade).where(Trade.client_order_id == kwargs["client_order_id"])
            ).one()
            assert row.status == "pending"
        self.placed.append(kwargs)
        await asyncio.sleep(0)
        return {
            "order": {
                "order_id": f"K-{kwargs['client_order_id']}",
                "client_order_id": kwargs["client_order_id"],
            }
        }


def trade(**kwargs):
    fields = {
        "client_order_id": f"m5mm-{uuid4()}",
        "ticker": "T",
        "side": "yes",
        "action": "sell",
        "price_cents": 5,
        "count": 10,
        "filled_count": None,
        "strategy": "motor_5_mm",
        "status": "pending",
    }
    fields.update(kwargs)
    return Trade(**fields)


def insert(*rows):
    with get_session() as session:
        session.add_all(rows)
        session.commit()


def quote(ticker="T", *, bid=None, ask=5, size=10):
    return QuoteSet(ticker=ticker, fair_prob=0.5, bid_cents=bid, ask_cents=ask, size=size)


@pytest.mark.parametrize("side", ["yes", "no"])
@pytest.mark.parametrize(
    "status,count,filled,expected", [("pending", 10, 2, 9.5), ("filled", 10, 2, 1.9)]
)
def test_sell_five_cents_reserves_ninety_five(risk, side, status, count, filled, expected):
    insert(trade(side=side, status=status, count=count, filled_count=filled))
    executor = Motor5Executor(FakeClient(), risk=risk)
    assert executor._mm_open_cost_usd() == pytest.approx(expected)
    assert risk._get_current_exposure_usd() == pytest.approx(expected)


@pytest.mark.parametrize(
    "invalid",
    [
        {"status": "filled", "filled_count": None},
        {"status": "filled", "filled_count": -1},
        {"status": "filled", "filled_count": 11},
        {"status": "mystery"},
        {"action": "mystery"},
        {"side": "mystery"},
        {"price_cents": 101},
        {"notes": COUNT_QUARANTINE_NOTE},
    ],
)
async def test_unknown_exposure_blocks_order(risk, invalid):
    insert(trade(**invalid))
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    assert math.isinf(executor._mm_open_cost_usd())
    assert math.isinf(risk._get_current_exposure_usd())
    assert await executor.sync_quotes(quote("OTHER")) == "risk_blocked"
    assert client.placed == []


async def test_sell_canary_cap_uses_complement(risk):
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk, max_exposure_usd=1.0)
    assert await executor.sync_quotes(quote(size=2)) == "risk_blocked"
    assert client.placed == []


async def test_parallel_tickers_cannot_share_canary_headroom(risk):
    client = FakeClient()
    first = Motor5Executor(client, risk=risk, max_exposure_usd=10.0)
    second = Motor5Executor(client, risk=RiskManager(), max_exposure_usd=10.0)
    outcomes = await asyncio.gather(first.sync_quotes(quote("A")), second.sync_quotes(quote("B")))
    assert sorted(outcomes) == ["risk_blocked", "synced"]
    assert len(client.placed) == 1
    assert risk._get_current_exposure_usd() == pytest.approx(9.5)


@pytest.mark.parametrize("case", ["missing", "stale", "floor", "raw_dropped", "nonfinite"])
async def test_unconfirmed_cash_cannot_authorize_new_risk(risk, monkeypatch, case):
    if case == "missing":
        monkeypatch.setattr(RiskManager, "_cached_capital_usd", None)
        monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", None)
        monkeypatch.setattr(RiskManager, "_last_balance_at", None)
    elif case == "stale":
        monkeypatch.setattr(RiskManager, "_last_balance_at", datetime.now(UTC) - timedelta(hours=1))
    elif case == "floor":
        monkeypatch.setattr(RiskManager, "_cached_capital_usd", 1.0)
        monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 1.0)
    elif case == "raw_dropped":
        monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 1.0)
    else:
        monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", float("nan"))
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    assert await executor.sync_quotes(quote(size=1)) == "risk_blocked"
    assert client.placed == []


async def test_missing_risk_argument_still_checks_cash(risk, monkeypatch):
    monkeypatch.setattr(RiskManager, "_last_balance_at", None)
    client = FakeClient()
    executor = Motor5Executor(client)
    assert await executor.sync_quotes(quote()) == "risk_blocked"
    assert client.placed == []


async def test_no_partial_sizing_without_changing_requested_quote(risk):
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    assert await executor.sync_quotes(quote(size=20)) == "risk_blocked"
    assert client.placed == []


def test_m5_has_no_hedge_discount_from_unattributed_notes(risk):
    notes = f"arb_id={uuid4()}"
    insert(
        trade(action="buy", side="yes", status="filled", filled_count=10, notes=notes),
        trade(action="buy", side="no", status="filled", filled_count=10, notes=notes),
    )
    assert risk._get_current_exposure_usd() == pytest.approx(1.0)


async def test_m5_and_other_atomic_reservation_share_lock(risk):
    risk.settings.MAX_SIMULTANEOUS_EXPOSURE_PCT = 5.0
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    opp = ArbOpportunity(
        legs=(ArbLeg("OTHER", "yes", 95, 10, 10),),
        count=10,
        gross_profit_cents=0,
        fees_cents=0,
        net_profit_cents=0,
        edge_pct=0,
    )

    def persist(decision):
        insert(
            trade(
                ticker="OTHER",
                strategy="motor_2_consensus",
                action="buy",
                price_cents=95,
                count=decision.max_allowed_count,
            )
        )
        return True

    await asyncio.gather(
        executor.sync_quotes(quote()), RiskManager().check_and_reserve(opp, persist)
    )
    assert risk._get_current_exposure_usd() <= 10.0


async def test_db_read_failure_sends_nothing(risk, monkeypatch):
    def fail():
        raise OSError("SQLite unavailable")

    monkeypatch.setattr("src.risk.manager.get_session", fail)
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    assert await executor.sync_quotes(quote()) == "risk_blocked"
    assert client.placed == []


async def test_quote_waits_for_shared_reservation_lock(risk):
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    async with RiskManager._check_lock:
        task = asyncio.create_task(executor.sync_quotes(quote()))
        await asyncio.sleep(0)
        assert not task.done()
        assert client.placed == []
        insert(trade(ticker="OTHER", count=50))  # $47.50 de los $50 globales.
    assert await task == "risk_blocked"
    assert client.placed == []


async def test_latch_that_appears_during_risk_wait_blocks_intent(risk, monkeypatch):
    original = risk._check_pre_trade_locked

    async def checked(*args, **kwargs):
        result = await original(*args, **kwargs)
        latch_incident("concurrent reconciliation uncertainty")
        return result

    monkeypatch.setattr(risk, "_check_pre_trade_locked", checked)
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    assert await executor.sync_quotes(quote()) == "risk_blocked"
    assert incident_status()[0]
    assert client.placed == []
    with get_session() as session:
        assert list(session.exec(select(Trade))) == []


@pytest.mark.parametrize("change", ["capital", "paused"])
async def test_gate_revalidates_after_await_before_intent(risk, monkeypatch, change):
    original = risk._check_pre_trade_locked

    async def checked(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "capital":
            monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 1.0)
        else:
            monkeypatch.setattr(BotState, "is_paused", True)
        return result

    monkeypatch.setattr(risk, "_check_pre_trade_locked", checked)
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    assert await executor.sync_quotes(quote()) == "risk_blocked"
    assert client.placed == []


async def test_intent_write_failure_latches_and_sends_nothing(risk, monkeypatch):
    def fail():
        raise OSError("intent DB unavailable")

    monkeypatch.setattr("src.strategies.motor_5_mm.executor.get_session", fail)
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    assert await executor.sync_quotes(quote()) == "corrupted"
    assert client.placed == []
    assert incident_status()[0]


async def test_latch_write_failure_is_not_hidden(risk, monkeypatch):
    def fail():
        raise OSError("intent DB unavailable")

    def fail_latch(reason):
        raise OSError("incident DB unavailable")

    monkeypatch.setattr("src.strategies.motor_5_mm.executor.get_session", fail)
    monkeypatch.setattr("src.strategies.motor_5_mm.executor.latch_incident", fail_latch)
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    with pytest.raises(OSError, match="incident DB unavailable"):
        await executor.sync_quotes(quote())
    assert client.placed == []
    assert executor.corrupted == {"T"}


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"order": None},
        {"order": {"order_id": ""}},
        {"order": {"order_id": 123}},
        {"order_id": "outer", "order": {"order_id": "inner"}},
        {"order": {"order_id": "K", "client_order_id": "not-our-order"}},
        {"order": {"order_id": "K", "side": "no"}},
        {"order": {"order_id": "K", "ticker": "OTHER"}},
    ],
)
async def test_invalid_place_response_keeps_reservation_and_stops_other_side(risk, response):
    client = FakeClient()

    async def place(**kwargs):
        client.placed.append(kwargs)
        return response

    client.place_order = place
    executor = Motor5Executor(client, risk=risk)
    assert await executor.sync_quotes(quote(bid=47, ask=53)) == "corrupted"
    assert len(client.placed) == 1
    assert len(executor.live_coids()) == 1
    assert incident_status()[0]
    with get_session() as session:
        rows = list(session.exec(select(Trade)))
        assert len(rows) == 1 and rows[0].status == "pending"
    restarted = Motor5Executor(client, risk=risk)
    assert await restarted.sync_quotes(quote("OTHER")) == "corrupted"
    assert len(client.placed) == 1


async def test_order_id_write_failure_keeps_owner_and_blocks_other_side(risk, monkeypatch):
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    monkeypatch.setattr(executor, "_set_kalshi_id", lambda *args: False)
    assert await executor.sync_quotes(quote(bid=47, ask=53)) == "corrupted"
    assert len(client.placed) == 1
    assert len(executor.live_order_ids()) == 1
    assert incident_status()[0]


async def test_cancelled_place_task_keeps_pending_and_latches(risk):
    client = FakeClient()
    entered = asyncio.Event()

    async def place(**kwargs):
        client.placed.append(kwargs)
        entered.set()
        await asyncio.Event().wait()

    client.place_order = place
    executor = Motor5Executor(client, risk=risk)
    task = asyncio.create_task(executor.sync_quotes(quote()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(executor.live_coids()) == 1
    assert risk._get_current_exposure_usd() == pytest.approx(9.5)
    assert incident_status()[0]


@pytest.mark.parametrize("status", ["pending", "filled", "cancelled", "settled"])
def test_bank200_daily_commitment_counts_sell_complement(risk, status):
    risk.settings.EXPERIMENT_BANK200_ENABLED = True
    risk.settings.EXPERIMENT_START_AT = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    insert(trade(status=status, filled_count=2))
    snap = risk._experiment_accounting_snapshot()
    assert snap["valid"]
    assert snap["daily_new_risk_usd"] == pytest.approx((950 + kalshi_fee_cents(10, 5)) / 100)


def test_binary_arb_discounts_confirmed_quantity_only(risk):
    notes = f"arb_id={uuid4()}"
    insert(
        trade(
            strategy="motor_rest_arb",
            action="buy",
            side="yes",
            price_cents=40,
            status="filled",
            count=10,
            filled_count=2,
            notes=notes,
        ),
        trade(
            strategy="motor_rest_arb",
            action="buy",
            side="no",
            price_cents=50,
            status="filled",
            count=10,
            filled_count=1,
            notes=notes,
        ),
    )
    assert risk._get_current_exposure_usd() == pytest.approx(0.4)


async def new_entry_decision(risk, api, persisted):
    opportunity = ArbOpportunity(
        legs=(ArbLeg("OTHER-MOTOR", "yes", 50, 1, 1),),
        count=1,
        gross_profit_cents=0,
        fees_cents=0,
        net_profit_cents=0,
        edge_pct=0,
    )

    def persist(*args):
        persisted.append(True)
        return True

    if api == "check":
        return await risk.check_pre_trade(opportunity)
    if api == "reserve":
        return await risk.check_and_reserve(opportunity, persist)
    return await risk.check_quote_and_reserve(trade(), persist)


@pytest.mark.parametrize("api", ["check", "reserve", "quote"])
async def test_phantom_incident_without_trade_blocks_shared_entry_gates(risk, api):
    latch_incident("phantom M5 sin fila: exposición pendiente de revisar")
    # La suma del ledger sigue siendo cero; no representa certeza del exchange.
    assert risk._get_current_exposure_usd() == 0
    persisted = []
    decision = await new_entry_decision(risk, api, persisted)
    assert not decision.approved
    assert "M5" in decision.reason
    assert persisted == []


@pytest.mark.parametrize("api", ["check", "reserve", "quote"])
async def test_clear_incident_allows_otherwise_valid_shared_entry(risk, api):
    with get_session() as session:
        session.add(OperationalState(key=INCIDENT_KEY, value="clear", reason="offline fixture"))
        session.commit()
    persisted = []
    decision = await new_entry_decision(risk, api, persisted)
    assert decision.approved
    assert persisted == ([] if api == "check" else [True])


@pytest.mark.parametrize("api", ["check", "reserve", "quote"])
async def test_unreadable_incident_blocks_shared_entry(risk, monkeypatch, api):
    def fail():
        raise OSError("incident status unavailable")

    monkeypatch.setattr("src.strategies.motor_5_mm.incidents.get_session", fail)
    persisted = []
    decision = await new_entry_decision(risk, api, persisted)
    assert not decision.approved
    assert "M5" in decision.reason
    assert persisted == []


@pytest.mark.parametrize("api", ["check", "reserve", "quote"])
async def test_incident_during_shared_gate_await_blocks_entry(risk, monkeypatch, api):
    async def stop_snapshot():
        await asyncio.sleep(0)
        latch_incident("M5 phantom detectado durante espera del gate")
        return None

    monkeypatch.setattr(risk, "_check_timeframe_stop_losses", stop_snapshot)
    persisted = []
    decision = await new_entry_decision(risk, api, persisted)
    assert not decision.approved
    assert persisted == []


@pytest.mark.parametrize("api", ["reserve", "quote"])
async def test_atomic_apis_recheck_incident_immediately_before_intent(risk, monkeypatch, api):
    original = risk._check_pre_trade_locked

    async def checked(*args, **kwargs):
        decision = await original(*args, **kwargs)
        latch_incident("M5 incierto tras terminar evaluación")
        return decision

    monkeypatch.setattr(risk, "_check_pre_trade_locked", checked)
    persisted = []
    decision = await new_entry_decision(risk, api, persisted)
    assert not decision.approved
    assert persisted == []


async def test_shared_incident_does_not_block_existing_order_cancellation(risk):
    client = FakeClient()
    executor = Motor5Executor(client, risk=risk)
    assert await executor.sync_quotes(quote()) == "synced"
    latch_incident("incidente global: entradas pausadas, cancelación sigue disponible")

    async def cancel_batch(order_ids):
        return {
            "orders": [
                {"order_id": order_id, "status": "canceled", "fill_count_fp": "0.00"}
                for order_id in order_ids
            ]
        }

    client.batch_cancel_orders = cancel_batch
    assert await executor.cancel_all("risk cleanup") == 1
    assert executor.live_coids() == set()
    assert risk._get_current_exposure_usd() == 0
    assert incident_status()[0]
