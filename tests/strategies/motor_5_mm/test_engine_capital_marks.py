"""Retiro al perder cash y marks de liquidación: clientes ficticios, SQLite temporal."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import select

from src.risk.manager import RiskManager
from src.storage.models import Trade, get_session
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.engine import Motor5Engine
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.strategies.motor_5_mm.reconciler import MMReconciler
from src.strategies.motor_5_mm.shadow_fill import ShadowFill
from tests.strategies.motor_5_mm.test_engine_f2 import _book, _LifecycleClient


@pytest.fixture(autouse=True)
def clean_risk_marks(monkeypatch):
    monkeypatch.setattr(RiskManager, "_marks", {})
    monkeypatch.setattr(RiskManager, "_unrealized_alert_date", None)


def live_engine(client, *, max_tickers=2):
    risk = RiskManager()
    engine = Motor5Engine(max_tickers=max_tickers, trading_enabled=True, risk_manager=risk)
    engine._client = client
    engine._executor = Motor5Executor(client, risk=risk)
    engine._reconciler = MMReconciler(client)
    return engine


def stale_cash():
    RiskManager._last_balance_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["stale", "missing", "below_floor", "nonfinite"])
async def test_same_quotes_are_cancelled_when_capital_permission_is_lost(loss):
    client = _LifecycleClient()
    client.cancel_fails = False
    client.books["T-A"] = _book(40, 60)
    engine = live_engine(client)
    FairValueBook.publish({"T-A": 0.5})
    await engine._tick()
    assert len(client.orders) == 2
    if loss == "stale":
        stale_cash()
    elif loss == "missing":
        RiskManager._last_raw_balance_usd = None
    elif loss == "below_floor":
        RiskManager._last_raw_balance_usd = 1
    else:
        RiskManager._last_raw_balance_usd = float("nan")
    await engine._tick()
    assert client.cancel_attempts == 2
    assert not engine._executor.live_coids()
    assert not engine._live_quotes
    with get_session() as session:
        assert all(row.status == "cancelled" for row in session.exec(select(Trade)))
    # Un retiro confirmado por cash no enclava corrupción artificial.
    RiskManager._last_raw_balance_usd = 1000
    RiskManager._last_balance_at = datetime.now(UTC).replace(tzinfo=None)
    await engine._tick()
    assert len(client.orders) == 4


@pytest.mark.asyncio
async def test_cancel_failure_keeps_pending_and_retries_without_capital():
    client = _LifecycleClient()
    client.books["T-A"] = _book(40, 60)
    engine = live_engine(client)
    FairValueBook.publish({"T-A": 0.5})
    await engine._tick()
    stale_cash()
    await engine._tick()
    assert len(engine._executor.live_coids()) == 2
    assert not engine._live_quotes
    assert engine._risk._get_current_exposure_usd() > 0
    with get_session() as session:
        assert all(row.status == "pending" for row in session.exec(select(Trade)))
    first_attempts = client.cancel_attempts
    client.cancel_fails = False
    await engine._tick()
    assert client.cancel_attempts > first_attempts
    assert not engine._executor.live_coids()
    assert len(client.orders) == 2


@pytest.mark.asyncio
async def test_fill_during_capital_cancel_is_applied_once():
    class FillOnCancel(_LifecycleClient):
        async def cancel_order(self, oid):
            result = await super().cancel_order(oid)
            order = next(order for order in self.orders if order["order_id"] == oid)
            if order["action"] == "buy":
                order.update(fill_count_fp="0.25", remaining_count_fp="0.00")
                result = {"order": dict(order)}
            return result

    client = FillOnCancel()
    client.cancel_fails = False
    client.books["T-A"] = _book(40, 60)
    engine = live_engine(client)
    FairValueBook.publish({"T-A": 0.5})
    await engine._tick()
    stale_cash()
    await engine._tick()
    assert engine._inventory.net("T-A") == Fraction(1, 4)
    assert engine._risk._get_current_exposure_usd() > 0
    await engine._tick()
    assert engine._inventory.net("T-A") == Fraction(1, 4)
    assert len(client.orders) == 2


@pytest.mark.asyncio
async def test_cash_expiring_during_book_read_retires_all_before_unchanged_sync():
    client = _LifecycleClient()
    client.cancel_fails = False
    client.books["T-A"] = _book(40, 60)
    engine = live_engine(client)
    FairValueBook.publish({"T-A": 0.5})
    await engine._tick()
    original = client.get_orderbook

    async def delayed_book(ticker):
        result = await original(ticker)
        stale_cash()
        return result

    client.get_orderbook = delayed_book
    await engine._tick()
    assert not engine._executor.live_coids()
    assert client.cancel_attempts == 2
    assert len(client.orders) == 2


def test_publish_marks_uses_executable_sides_and_original_book_time(monkeypatch):
    engine = live_engine(_LifecycleClient())
    recorded = Mock()
    monkeypatch.setattr(RiskManager, "record_mark", recorded)
    observed = datetime(2026, 10, 6, 16, tzinfo=UTC)
    engine._publish_risk_mark(
        "T", 40, 62, observed_at=observed, fee=SimpleNamespace(multiplier=Fraction(1, 2))
    )
    assert [call.args for call in recorded.call_args_list] == [("T", "yes", 40), ("T", "no", 38)]
    assert [call.kwargs["ask_cents"] for call in recorded.call_args_list] == [62, 60]
    assert all(call.kwargs["observed_at"] == observed for call in recorded.call_args_list)
    assert all(call.kwargs["fee_multiplier"] == Fraction(1, 2) for call in recorded.call_args_list)
    engine._invalidate_mark("T")
    assert [call.args for call in recorded.call_args_list[-2:]] == [
        ("T", "yes", None),
        ("T", "no", None),
    ]


@pytest.mark.asyncio
async def test_position_marks_outside_fair_rotate_with_bounded_reads(monkeypatch):
    client = _LifecycleClient()
    engine = live_engine(client, max_tickers=1)
    engine._risk.settings.UNREALIZED_STOP_ENABLED = True
    engine._fee_policy = SimpleNamespace(
        observe=AsyncMock(return_value=SimpleNamespace(multiplier=1))
    )
    recorded = Mock()
    monkeypatch.setattr(RiskManager, "record_mark", recorded)
    for ticker in ("T-A", "T-B"):
        client.books[ticker] = _book(40, 60)
        engine._inventory.apply_fill(
            ShadowFill(ticker=ticker, side="buy", price_cents=50, count=1, rule="real_fill")
        )
    client.get_market = AsyncMock(
        side_effect=lambda t: {"market": {"ticker": t, "event_ticker": "T"}}
    )
    await engine._refresh_position_risk_marks({})
    assert client.get_market.await_count == 1
    assert recorded.call_args_list[0].args == ("T-A", "yes", 40)
    await engine._refresh_position_risk_marks({})
    assert client.get_market.await_count == 2
    assert recorded.call_args_list[2].args == ("T-B", "yes", 40)
    engine._fee_policy.observe.assert_awaited_with("T-B", event_ticker="T")


async def engine_with_partial_fill():
    client = _LifecycleClient()
    client.cancel_fails = False
    client.books["T-A"] = _book(40, 60)
    engine = live_engine(client)
    engine._fee_policy = SimpleNamespace(
        observe=AsyncMock(return_value=SimpleNamespace(multiplier=1))
    )
    FairValueBook.publish({"T-A": 0.5}, event_tickers={"T-A": "T"})
    await engine._tick()
    buy = next(order for order in client.orders if order["action"] == "buy")
    buy.update(
        fill_count_fp="0.25",
        maker_fill_cost_dollars="0.107500",
        taker_fill_cost_dollars="0.000000",
        maker_fees_dollars="0.000125",
        taker_fees_dollars="0.000000",
    )
    engine._risk.settings.UNREALIZED_STOP_ENABLED = True
    return engine, client, buy


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["loss", "fees_missing", "book_missing"])
async def test_mtm_block_retires_unchanged_quotes_after_refresh(block):
    engine, client, buy = await engine_with_partial_fill()
    if block == "loss":
        engine._risk.settings.MAX_UNREALIZED_LOSS_PCT = 0
        engine._risk.settings.MAX_UNREALIZED_LOSS_FLOOR_USD = 0.01
    elif block == "fees_missing":
        del buy["maker_fees_dollars"]
    else:
        del client.books["T-A"]
    await engine._tick()
    assert client.cancel_attempts == 2
    assert not engine._executor.live_coids()
    assert not engine._live_quotes
    assert len(client.orders) == 2
    assert engine._inventory.net("T-A") == Fraction(1, 4)
    assert engine._risk.unrealized_entry_block_reason() is not None
    if block == "book_missing":
        # El bloqueo no corta el refresh: vuelve a cubrir la posición y cotizar.
        client.books["T-A"] = _book(40, 60)
        await engine._tick()
        assert engine._risk.unrealized_entry_block_reason() is None
        assert len(client.orders) == 4


@pytest.mark.asyncio
async def test_mtm_complete_mark_fee_survives_regular_quote_path():
    engine, _, _ = await engine_with_partial_fill()
    await engine._tick()
    snap = engine._risk._unrealized_snapshot()
    assert not snap["unknown"]
    assert snap["covered"] == 1
    assert engine._risk.unrealized_entry_block_reason() is None


@pytest.mark.asyncio
async def test_mtm_cancel_failure_retains_reserve_and_retries():
    engine, client, _ = await engine_with_partial_fill()
    client.cancel_fails = True
    del client.books["T-A"]
    await engine._tick()
    assert engine._executor.live_coids()
    assert engine._risk._get_current_exposure_usd() > 0
    attempts = client.cancel_attempts
    client.cancel_fails = False
    await engine._tick()
    assert client.cancel_attempts > attempts
    assert not engine._executor.live_coids()
    assert engine._inventory.net("T-A") == Fraction(1, 4)


@pytest.mark.asyncio
async def test_mark_becoming_unavailable_during_book_read_retires_before_sync(monkeypatch):
    client = _LifecycleClient()
    client.cancel_fails = False
    client.books["T-A"] = _book(40, 60)
    engine = live_engine(client)
    FairValueBook.publish({"T-A": 0.5})
    await engine._tick()
    original = client.get_orderbook
    unavailable = False

    async def delayed_book(ticker):
        nonlocal unavailable
        result = await original(ticker)
        unavailable = True
        return result

    client.get_orderbook = delayed_book
    monkeypatch.setattr(
        engine._risk, "unrealized_entry_block_reason", lambda: "MTM" if unavailable else None
    )
    await engine._tick()
    assert client.cancel_attempts == 2
    assert not engine._executor.live_coids()
    assert len(client.orders) == 2
