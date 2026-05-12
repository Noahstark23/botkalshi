"""Tests para src/strategies/motor_1_arbitrage/executor.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import SQLModel, create_engine, select

import src.storage.models as _models
from src.math.arbitrage import detect_binary_arb
from src.monitoring.health import BotState
from src.risk.manager import RiskManager, TradeDecision
from src.storage.models import RiskEvent, Trade, get_session
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor

# =====================================================
# Fixtures
# =====================================================


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Replace global engine with in-memory SQLite for each test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(_models, "_engine", engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    monkeypatch.setattr(_models, "_engine", None)


@pytest.fixture(autouse=True)
def reset_bot_state():
    BotState.is_paused = False
    BotState.pause_reason = None
    yield
    BotState.is_paused = False
    BotState.pause_reason = None


@pytest.fixture
def mock_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def mock_risk_manager() -> MagicMock:
    rm = MagicMock(spec=RiskManager)
    rm.check_pre_trade = AsyncMock(
        return_value=TradeDecision(approved=True, reason="ok", max_allowed_count=10)
    )
    return rm


@pytest.fixture
def executor(mock_client, mock_risk_manager) -> ArbitrageExecutor:
    return ArbitrageExecutor(mock_client, mock_risk_manager)


@pytest.fixture
def binary_opp():
    opp = detect_binary_arb("MKT-A", 40, 200, 45, 200, max_count=10)
    assert opp is not None
    return opp


def _mock_settings(trading_enabled: bool = True) -> MagicMock:
    s = MagicMock()
    s.TRADING_ENABLED = trading_enabled
    s.ACTIVE_CAPITAL_USD = 300.0
    s.MAX_TRADE_SIZE_PCT = 5.0
    return s


# =====================================================
# Dry-run
# =====================================================


@pytest.mark.asyncio
async def test_dry_run_skips_network(executor, mock_client, binary_opp):
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_mock_settings(trading_enabled=False),
    ):
        result = await executor.execute(binary_opp)

    assert result is True
    mock_client.place_order.assert_not_called()

    # _persist_intents not called → no trades in DB
    with get_session() as s:
        trades = s.exec(select(Trade)).all()
    assert len(trades) == 0


# =====================================================
# Happy path — all legs fill
# =====================================================


@pytest.mark.asyncio
async def test_all_legs_fill_returns_true(executor, mock_client, binary_opp):
    mock_client.place_order.return_value = {"order": {"order_id": "kalshi-001"}}

    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_mock_settings(trading_enabled=True),
    ):
        result = await executor.execute(binary_opp)

    assert result is True
    assert mock_client.place_order.call_count == 2

    with get_session() as s:
        trades = list(s.exec(select(Trade)).all())
    assert len(trades) == 2
    assert all(t.status == "filled" for t in trades)
    assert all(t.filled_at is not None for t in trades)


# =====================================================
# Partial fill + rollback
# =====================================================


@pytest.mark.asyncio
async def test_partial_fill_triggers_rollback(executor, mock_client, binary_opp):
    # YES leg succeeds, NO leg raises
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes"}},  # YES buy succeeds
        Exception("network error"),  # NO buy fails
        {"order": {}},  # YES rollback sell succeeds
    ]
    mock_client.get_orderbook.return_value = {
        "orderbook": {"yes": [[38, 10], [35, 5]], "no": [[44, 8]]}
    }

    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_mock_settings(trading_enabled=True),
    ):
        result = await executor.execute(binary_opp)

    assert result is False

    with get_session() as s:
        trades = list(s.exec(select(Trade)).all())

    # YES leg should be rolled_back; NO leg stays pending (never filled)
    by_side = {t.side: t for t in trades}
    assert by_side["yes"].status == "rolled_back"
    assert by_side["no"].status == "pending"


@pytest.mark.asyncio
async def test_rollback_uses_aggressive_price_1(executor, mock_client, binary_opp):
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes"}},  # YES buy succeeds
        Exception("fail"),  # NO buy fails
        {"order": {}},  # YES rollback sell succeeds
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[38, 10]], "no": []}}

    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_mock_settings(trading_enabled=True),
    ):
        await executor.execute(binary_opp)

    # Find the sell call (place_order called 3 times: buy YES, buy NO, sell YES)
    sell_calls = [
        call
        for call in mock_client.place_order.call_args_list
        if call.kwargs.get("action") == "sell"
    ]
    assert len(sell_calls) == 1
    assert sell_calls[0].kwargs["yes_price"] == 1
    assert sell_calls[0].kwargs["side"] == "yes"


@pytest.mark.asyncio
async def test_rollback_aborts_on_excessive_slippage(executor, mock_client, binary_opp):
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes"}},
        Exception("fail"),
    ]
    # YES bid = 5¢, original price = 40¢ → slippage = (40-5)/40*100 = 87.5% > 10%
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[5, 10]], "no": []}}

    with (
        patch(
            "src.strategies.motor_1_arbitrage.executor.get_settings",
            return_value=_mock_settings(trading_enabled=True),
        ),
        patch(
            "src.strategies.motor_1_arbitrage.executor.alert_error", new_callable=AsyncMock
        ) as mock_alert,
    ):
        await executor.execute(binary_opp)

    mock_alert.assert_awaited_once()
    # Sell order not placed
    sell_calls = [
        c for c in mock_client.place_order.call_args_list if c.kwargs.get("action") == "sell"
    ]
    assert len(sell_calls) == 0

    # YES trade NOT updated to rolled_back
    with get_session() as s:
        trades = list(s.exec(select(Trade)).all())
    by_side = {t.side: t for t in trades}
    assert by_side["yes"].status != "rolled_back"


# =====================================================
# Circuit breaker
# =====================================================


@pytest.mark.asyncio
async def test_circuit_breaker_pauses_after_threshold(executor):
    # Pre-seed 2 rollback events (threshold=3, so 3rd call triggers)
    with get_session() as s:
        for _ in range(2):
            s.add(
                RiskEvent(
                    event_type="atomic_rollback",
                    severity="warning",
                    message="Partial fill triggered rollback",
                )
            )
        s.commit()

    with patch(
        "src.strategies.motor_1_arbitrage.executor.alert_risk_event",
        new_callable=AsyncMock,
    ) as mock_alert:
        await executor._check_circuit_breaker()

    assert BotState.is_paused is True
    assert "circuit_breaker" in (BotState.pause_reason or "")
    mock_alert.assert_awaited_once()


# =====================================================
# Reconciliation
# =====================================================


@pytest.mark.asyncio
async def test_reconcile_no_pending_trades_no_op(executor, mock_client):
    await executor.reconcile_pending_trades()
    mock_client.get_orders.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_matches_executed_kalshi_order(executor, mock_client):
    coid = "test-coid-001"
    with get_session() as s:
        trade = Trade(
            client_order_id=coid,
            ticker="MKT-A",
            side="yes",
            action="buy",
            count=5,
            price_cents=40,
            strategy="motor_1_arbitrage",
            status="pending",
            placed_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        s.add(trade)
        s.commit()

    mock_client.get_orders.return_value = {
        "orders": [
            {
                "client_order_id": coid,
                "order_id": "kalshi-xyz",
                "status": "executed",
            }
        ]
    }

    await executor.reconcile_pending_trades()

    with get_session() as s:
        t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
    assert t is not None
    assert t.status == "filled"
    assert t.filled_at is not None
    assert t.kalshi_order_id == "kalshi-xyz"


@pytest.mark.asyncio
async def test_reconcile_marks_missing_as_error_missing(executor, mock_client):
    coid = "missing-coid-999"
    with get_session() as s:
        trade = Trade(
            client_order_id=coid,
            ticker="MKT-B",
            side="no",
            action="buy",
            count=3,
            price_cents=45,
            strategy="motor_1_arbitrage",
            status="pending",
            placed_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        s.add(trade)
        s.commit()

    # Kalshi returns empty orders — trade not found
    mock_client.get_orders.return_value = {"orders": []}

    with patch(
        "src.strategies.motor_1_arbitrage.executor.alert_error",
        new_callable=AsyncMock,
    ) as mock_alert:
        await executor.reconcile_pending_trades()

    mock_alert.assert_awaited_once()

    with get_session() as s:
        t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
    assert t is not None
    assert t.status == "error_missing"


@pytest.mark.asyncio
async def test_reconcile_marks_canceled_trade(executor, mock_client):
    coid = "canceled-001"
    with get_session() as s:
        trade = Trade(
            client_order_id=coid,
            ticker="MKT-D",
            side="yes",
            action="buy",
            count=2,
            price_cents=40,
            strategy="motor_1_arbitrage",
            status="pending",
            placed_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        s.add(trade)
        s.commit()

    mock_client.get_orders.return_value = {
        "orders": [{"client_order_id": coid, "order_id": "k-x", "status": "canceled"}]
    }
    await executor.reconcile_pending_trades()

    with get_session() as s:
        t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
    assert t is not None
    assert t.status == "cancelled"


@pytest.mark.asyncio
async def test_reconcile_marks_unknown_status_as_error(executor, mock_client):
    coid = "unknown-001"
    with get_session() as s:
        trade = Trade(
            client_order_id=coid,
            ticker="MKT-E",
            side="no",
            action="buy",
            count=2,
            price_cents=45,
            strategy="motor_1_arbitrage",
            status="pending",
            placed_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        s.add(trade)
        s.commit()

    mock_client.get_orders.return_value = {
        "orders": [{"client_order_id": coid, "order_id": "k-y", "status": "resting"}]
    }
    await executor.reconcile_pending_trades()

    with get_session() as s:
        t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
    assert t is not None
    assert t.status == "error"


@pytest.mark.asyncio
async def test_reconcile_get_orders_exception_returns_early(executor, mock_client):
    coid = "ex-001"
    with get_session() as s:
        trade = Trade(
            client_order_id=coid,
            ticker="MKT-F",
            side="yes",
            action="buy",
            count=1,
            price_cents=40,
            strategy="motor_1_arbitrage",
            status="pending",
            placed_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        s.add(trade)
        s.commit()

    mock_client.get_orders.side_effect = Exception("API down")
    await executor.reconcile_pending_trades()  # should not raise

    # Trade still pending (not updated)
    with get_session() as s:
        t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
    assert t is not None
    assert t.status == "pending"


# =====================================================
# execute — risk rejected
# =====================================================


@pytest.mark.asyncio
async def test_execute_returns_false_when_risk_rejected(executor, mock_client, binary_opp):
    executor.risk_manager.check_pre_trade = AsyncMock(
        return_value=TradeDecision(
            approved=False, reason="capital cap: allowed=0", max_allowed_count=0
        )
    )
    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_mock_settings(trading_enabled=True),
    ):
        result = await executor.execute(binary_opp)

    assert result is False
    mock_client.place_order.assert_not_called()


# =====================================================
# initialize
# =====================================================


@pytest.mark.asyncio
async def test_initialize_calls_reconcile(executor, mock_client):
    # No pending trades → reconcile returns early, get_orders not called
    await executor.initialize()
    mock_client.get_orders.assert_not_called()


# =====================================================
# _update_trade_status — filled path sets filled_at
# =====================================================


def test_update_trade_status_filled_sets_filled_at(executor):
    coid = "fill-001"
    with get_session() as s:
        trade = Trade(
            client_order_id=coid,
            ticker="MKT-G",
            side="yes",
            action="buy",
            count=1,
            price_cents=40,
            strategy="motor_1_arbitrage",
            status="pending",
        )
        s.add(trade)
        s.commit()

    executor._update_trade_status(coid, "filled")

    with get_session() as s:
        t = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
    assert t is not None
    assert t.status == "filled"
    assert t.filled_at is not None


# =====================================================
# Rollback — no bid available
# =====================================================


@pytest.mark.asyncio
async def test_rollback_skips_leg_when_no_bid(mock_client, mock_risk_manager, binary_opp):
    # Use max_rollback_retries=1 to keep test fast (1 sleep instead of 3)
    ex = ArbitrageExecutor(mock_client, mock_risk_manager, max_rollback_retries=1)
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes"}},
        Exception("fail"),
    ]
    # No bids available on any side
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [], "no": []}}

    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_mock_settings(trading_enabled=True),
    ):
        result = await ex.execute(binary_opp)

    assert result is False
    # No sell order placed (no bid to cross)
    sell_calls = [
        c for c in mock_client.place_order.call_args_list if c.kwargs.get("action") == "sell"
    ]
    assert len(sell_calls) == 0


@pytest.mark.asyncio
async def test_rollback_retries_on_sell_exception(mock_client, mock_risk_manager, binary_opp):
    """Verify the except block fires when the sell call itself raises."""
    ex = ArbitrageExecutor(mock_client, mock_risk_manager, max_rollback_retries=1)
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes"}},  # YES buy succeeds
        Exception("no buy"),  # NO buy fails
        Exception("sell failed"),  # rollback sell also fails
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[38, 10]], "no": []}}

    with patch(
        "src.strategies.motor_1_arbitrage.executor.get_settings",
        return_value=_mock_settings(trading_enabled=True),
    ):
        result = await ex.execute(binary_opp)

    assert result is False
    # All 3 place_order calls were made
    assert mock_client.place_order.call_count == 3


@pytest.mark.asyncio
async def test_reconcile_skips_recent_trades(executor, mock_client):
    # Trade placed 5s ago — within 30s window, should not be touched
    with get_session() as s:
        trade = Trade(
            client_order_id="recent-001",
            ticker="MKT-C",
            side="yes",
            action="buy",
            count=1,
            price_cents=40,
            strategy="motor_1_arbitrage",
            status="pending",
            placed_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        s.add(trade)
        s.commit()

    await executor.reconcile_pending_trades()
    mock_client.get_orders.assert_not_called()
